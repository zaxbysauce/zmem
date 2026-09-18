"""Expanded issue #183 acceptance checks (AC1--AC7).

These are deliberately independent unittest checks.  They describe the
cross-workstream contract at the store/host boundaries and are expected to
fail on a pre-implementation checkout because the new surfaces do not exist.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock
from contextlib import contextmanager


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
BODY_PY = ROOT / "hooks" / "lib" / "zmem-recall-body.py"
EXPECTED_NEGATIVE = ROOT / "tests" / "fixtures" / "injection-parity" / "expected-envelope.json"
AC6_BASE_WIRE = ROOT / "tests" / "fixtures" / "injection-parity" / "ac6-base-wire-lf.json"


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD",
        "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST", "ZMEM_SESSION", "ZMEM_INJECT",
        "ZMEM_QUERY_CONTEXT", "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(tmp / "store.sqlite"),
        "ZMEM_DATA": str(tmp / "data"),
        "ZMEM_MODELS_DIR": str(tmp / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_HOME": str(ROOT),
        "ZMEM_NAMESPACE": "project:ambiguity",
        "ZMEM_HOST": "claude",
        "ZMEM_QUERY_CONTEXT": "1",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


def _load(path: Path, prefix: str):
    spec = importlib.util.spec_from_file_location(prefix + uuid.uuid4().hex, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_provider():
    agent = types.ModuleType("agent")
    provider_mod = types.ModuleType("agent.memory_provider")
    provider_mod.MemoryProvider = type("MemoryProvider", (), {})
    agent.memory_provider = provider_mod
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", provider_mod)
    return _load(ROOT / "hermes-plugin" / "__init__.py", "issue183_provider_")


@contextmanager
def _stdin(text: str):
    old = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = old


class Issue183QueryAcceptance(unittest.TestCase):
    def test_ac1_classifier_oracle(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac1-") as raw:
            tmp = Path(raw)
            with mock.patch.dict(os.environ, _env(tmp), clear=True):
                if str(SCRIPTS) not in sys.path:
                    sys.path.insert(0, str(SCRIPTS))
                from storelib.query_ambiguity import is_ambiguous_prompt

                cases = [
                    ("", True), ("please help", True), ("fix failing test", True),
                    ("find memory issue", True), ("deploy service production", True),
                    ("continue from yesterday", True),
                    ("database migration rollback plan", False),
                    ("customer invoice reconciliation", True),
                    ("skills/memory/scripts/store.py", False), ("recall memory", True),
                    ("storelib.schema", False), ("memory_entity", False),
                    ("project::alpha", False), ("--dry-run", False),
                    ("ValueError", False), ("RuntimeException", False),
                    ("one two three four", False), ("the and of to", True),
                    ("alpha beta gamma", True),
                    ("read tests/test_ops_tokens.py now", False),
                ]
                self.assertEqual([is_ambiguous_prompt(q) for q, _ in cases], [x[1] for x in cases])
                self.assertTrue(is_ambiguous_prompt("one two", min_terms=3))
                self.assertFalse(is_ambiguous_prompt("one two", min_terms=2))
                os.environ["ZMEM_AMBIG_MIN_TERMS"] = "bad"
                self.assertTrue(is_ambiguous_prompt("one two three"))
                os.environ["ZMEM_AMBIG_MIN_TERMS"] = "0"
                self.assertTrue(is_ambiguous_prompt("one two three"))

    def test_ac2_bounded_rewrite_fixture(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac2-") as raw:
            with mock.patch.dict(os.environ, _env(Path(raw)), clear=True):
                if str(SCRIPTS) not in sys.path:
                    sys.path.insert(0, str(SCRIPTS))
                from storelib.query_ambiguity import rewrite_ambiguous_query

                query, rewritten = rewrite_ambiguous_query(
                    "continue from yesterday",
                    ops_tokens=["git", "status", "python", "-m", "unittest",
                                "tests/test_ops_tokens.py", "failed", "retry",
                                "store.py", "recall", "pretool", "session-183"],
                    edited_basenames=["recall.py", "ops_tokens.py", "README.md"],
                )
                expected = (
                    "continue from yesterday git status python -m unittest "
                    "tests/test_ops_tokens.py failed retry store.py recall pretool "
                    "session-183 recall.py ops_tokens.py README.md"
                )
                self.assertEqual((query, rewritten), (expected, True))
                self.assertLessEqual(len(query), 500)
                query, rewritten = rewrite_ambiguous_query(
                    "x" * 900,
                    ops_tokens=["a", "a", "b", "c"],
                    edited_basenames=["z.py", "z.py", "a.py"],
                )
                self.assertLessEqual(len(query), 500)
                self.assertTrue(rewritten)
                self.assertEqual(query.count(" a "), 1)
                self.assertEqual(query.count(" z.py"), 1)

    def test_ac3_exact_tokens_bypass(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac3-") as raw:
            with mock.patch.dict(os.environ, _env(Path(raw)), clear=True):
                if str(SCRIPTS) not in sys.path:
                    sys.path.insert(0, str(SCRIPTS))
                from storelib.query_ambiguity import rewrite_ambiguous_query

                for prompt in (
                    "open src/main.py", "inspect storelib.schema", "read memory_entity",
                    "query project::alpha", "run --dry-run", "trace ValueError",
                    "trace RuntimeException",
                ):
                    with self.subTest(prompt=prompt):
                        self.assertEqual(
                            rewrite_ambiguous_query(
                                prompt, ops_tokens=["new-context"], edited_basenames=["new.py"]
                            ),
                            (prompt, False),
                        )

    def test_ac4_evidence_read_is_newest_first_and_fail_open(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac4-") as raw:
            tmp = Path(raw)
            with mock.patch.dict(os.environ, _env(tmp), clear=True):
                if str(SCRIPTS) not in sys.path:
                    sys.path.insert(0, str(SCRIPTS))
                from storelib.query_ambiguity import read_recent_edit_basenames

                conn = sqlite3.connect(tmp / "evidence.sqlite", timeout=0.05)
                conn.execute("PRAGMA foreign_keys=ON")
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                conn.execute(
                    "CREATE TABLE evidence (id TEXT, session_id TEXT, kind TEXT, ts TEXT, ref_path TEXT)"
                )
                conn.executemany(
                    "INSERT INTO evidence VALUES (?,?,?,?,?)",
                    [("b", "s", "edit", "2026-09-10T00:00:01Z", "C:/new/b.py"),
                     ("a", "s", "edit", "2026-09-10T00:00:01Z", "C:/old/a.py"),
                     ("c", "s", "edit", "2026-09-09T00:00:01Z", "c.py"),
                     ("x", "other", "edit", "2026-09-11T00:00:01Z", "x.py")]
                )
                conn.commit()
                self.assertEqual(read_recent_edit_basenames(conn, "s"), ["b.py", "a.py", "c.py"])
                before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
                conn.execute("DROP TABLE evidence")
                conn.commit()
                self.assertEqual(read_recent_edit_basenames(conn, "s"), [])
                self.assertEqual(before[0][0].startswith("CREATE TABLE"), True)
                conn.close()
                closed = sqlite3.connect(":memory:")
                closed.close()
                self.assertEqual(read_recent_edit_basenames(closed, "s"), [])

    def test_ac5_hook_hermes_byte_parity_and_kill_switch(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac5-") as raw:
            tmp = Path(raw)
            env = _env(tmp)
            with mock.patch.dict(os.environ, env, clear=True):
                body = _load(BODY_PY, "issue183_body_")
                hook_calls: list[list[str]] = []

                def hook_store(store_py, args, timeout=None):
                    hook_calls.append(list(args))
                    if args[0] == "query-rewrite":
                        return subprocess.CompletedProcess(args, 0, '{"query":"continue from yesterday git status","rewrite":1}\n', "")
                    return subprocess.CompletedProcess(args, 0, '{"rendered":"shared-bytes","results":[],"reason":"injected"}\n', "")

                body._run_store = hook_store
                old_argv = sys.argv[:]
                try:
                    sys.argv = [str(BODY_PY), str(STORE_PY), "project:ambiguity", "1500", "user_prompt"]
                    hook_output = io.StringIO()
                    with _stdin(json.dumps({"prompt": "continue from yesterday", "session_id": "s"})), contextlib.redirect_stdout(hook_output):
                        body.main()
                finally:
                    sys.argv = old_argv
                hook_query = next(a[a.index("--query") + 1] for a in hook_calls if a[0] == "recall")
                self.assertEqual(hook_query, "continue from yesterday git status")
                hook_envelope = json.loads(hook_output.getvalue())
                hook_rendered = hook_envelope.get("additionalContext", "")
                self.assertEqual(hook_rendered, "shared-bytes")

                provider_mod = _load_provider()
                provider_calls: list[list[str]] = []

                def provider_store(args, timing=None, input_text=None):
                    provider_calls.append(list(args))
                    if args[0] == "query-rewrite":
                        return {"ok": True, "stdout": '{"query":"continue from yesterday git status","rewrite":1}\n'}
                    return {"ok": True, "stdout": '{"rendered":"shared-bytes"}\n'}

                provider_mod._run_store = provider_store
                provider = provider_mod.ZmemMemoryProvider()
                provider._namespace = "project:ambiguity"
                provider_rendered = provider.prefetch("continue from yesterday", session_id="s")
                self.assertEqual(provider_rendered, hook_rendered)
                provider_query = next(a[a.index("--query") + 1] for a in provider_calls if a[0] == "recall")
                self.assertEqual(provider_query, hook_query)

                os.environ["ZMEM_QUERY_CONTEXT"] = "0"
                hook_calls.clear()
                try:
                    sys.argv = [str(BODY_PY), str(STORE_PY), "project:ambiguity", "1500", "user_prompt"]
                    with _stdin(json.dumps({"prompt": "continue from yesterday", "session_id": "s"})), contextlib.redirect_stdout(io.StringIO()):
                        body.main()
                finally:
                    sys.argv = old_argv
                disabled_recalls = [a for a in hook_calls if a[0] == "recall"]
                self.assertTrue(disabled_recalls)
                disabled_query = disabled_recalls[-1][disabled_recalls[-1].index("--query") + 1]
                self.assertEqual(disabled_query, "continue from yesterday")
                self.assertFalse(any(a[0] == "query-rewrite" for a in hook_calls))

                provider_calls.clear()
                self.assertEqual(provider.prefetch("continue from yesterday", session_id="s"), hook_rendered)
                provider_disabled_recalls = [a for a in provider_calls if a[0] == "recall"]
                self.assertTrue(provider_disabled_recalls)
                provider_disabled_query = provider_disabled_recalls[-1][provider_disabled_recalls[-1].index("--query") + 1]
                self.assertEqual(provider_disabled_query, "continue from yesterday")
                self.assertFalse(any(a[0] == "query-rewrite" for a in provider_calls))

    def test_ac6_logging_process_bound_and_negative_control(self):
        with tempfile.TemporaryDirectory(prefix="zmem-183-ac6-") as raw:
            tmp = Path(raw)
            with mock.patch.dict(os.environ, _env(tmp), clear=True):
                body = _load(BODY_PY, "issue183_log_")
                body._maybe_log_drift = lambda *_: None
                body._rotate_telemetry_logs = lambda *_: None
                body._log_inject_decision([], [], "silent", "empty-pool", session_id="s", moment="user_prompt", rewrite=1)
                body._log_inject_decision([], [], "silent", "empty-pool", session_id="s", moment="user_prompt", rewrite=0)
                lines = (tmp / "zmem-decisions.log").read_text(encoding="utf-8").splitlines()
                self.assertTrue(any("rewrite=1" in line for line in lines))
                self.assertFalse(any("rewrite=0" in line for line in lines))

                fixture_store = tmp / "parity.sqlite"
                gen = ROOT / "tests" / "fixtures" / "injection-parity" / "generate.py"
                expected = tmp / "expected.json"
                fixed_env = _env(tmp, ZMEM_TEST_NOW="2026-06-01T00:00:00Z")
                proc = subprocess.run([sys.executable, str(gen), "--store", str(fixture_store), "--expected", str(expected)], env=fixed_env, capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = subprocess.run([sys.executable, str(STORE_PY), "recall", "--query", "stash pop", "--namespace", "project:parity", "--limit", "5", "--include-global", "--global-limit", "3", "--no-bump", "--for-injection", "--json", "--session-id", "phase25-parity-session", "--moment", "user_prompt", "--lane", "claude"], env=dict(fixed_env, ZMEM_STORE=str(fixture_store), ZMEM_QUERY_CONTEXT="0"), capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.encode(), AC6_BASE_WIRE.read_bytes())
                self.assertEqual(json.loads(result.stdout), json.loads(EXPECTED_NEGATIVE.read_bytes()))

    def test_ac7_release_parity_is_exact_current_version(self):
        manifests = [
            "marketplace.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
            ".codex-plugin/plugin.json", ".zcode-plugin/plugin.json",
            ".agents/plugins/marketplace.json", "hermes-plugin/plugin.yaml",
        ]
        values = []
        for relative in manifests:
            text = (ROOT / relative).read_text(encoding="utf-8")
            if relative.endswith(".json"):
                obj = json.loads(text)
                values.append(obj.get("version") or obj.get("plugins", [{}])[0].get("version"))
            else:
                values.append(next(line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("version:")))
        manifest = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
        expected_version = manifest.get("version")
        self.assertTrue(isinstance(expected_version, str) and expected_version)
        self.assertEqual(values, [expected_version] * len(manifests))
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(changelog, rf"(?m)^## \[{re.escape(expected_version)}\](?:\s|$)")
        gate = subprocess.run([sys.executable, str(ROOT / "scripts" / "release_gate.py")], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(gate.returncode, 0, gate.stderr + gate.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
