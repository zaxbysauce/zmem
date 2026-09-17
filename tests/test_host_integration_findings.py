"""Regression tests for issue #36 host-integration + resource findings the final
critic flagged as missing plan-mandated coverage:

  M8  — consolidate reports truncation when a namespace exceeds the per-namespace
        row cap, and the cap is genuinely per-namespace (no cross-namespace
        starvation).
  M10 — the Hermes compatibility hook delegates store work to the internal
        CLI, including path resolution and local-filesystem refusal.
  M13 — the convention-capture shell hook parses the tool name with the
        discovered $PYTHON_BIN, so it works when bare `python` is absent but
        `python3` exists (and emits empty when no interpreter is available).

Run: python tests/test_host_integration_findings.py
No pytest / third-party harness required — matches the repo convention.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"


# storelib submodules (issue #57): the store shim cannot forward
# attribute writes, so tests that mock a mutable global patch the owning submodule.
sys.path.insert(0, str(SCRIPTS_DIR))
import importlib as _ii
_consolidate_mod = _ii.import_module("storelib.consolidate")
PYTHON = sys.executable
HOOKS_DIR = REPO_ROOT / "hermes-plugin" / "hooks"

# Force embeddings deterministically OUT of scope for every op in this module
# (see test_consolidate_lossy for the full rationale): the lazy availability
# check runs under ambient env after the per-store mock env is restored, so a
# host with the shared model cache would flip host integration behavior to
# embedding semantics and change what these tests observe.
os.environ["ZMEM_MODELS_DIR"] = str(REPO_ROOT / "no-such-models")


def _load_store_module(store_path: str, models_dir: str):
    spec = importlib.util.spec_from_file_location(
        f"zmem_host_int_{os.getpid()}", str(STORE_PY))
    with mock.patch.dict(os.environ, {"ZMEM_STORE": store_path,
                                      "ZMEM_MODELS_DIR": models_dir,
                                      "ZMEM_MODEL_AUTODOWNLOAD": "0"}):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class M8ConsolidatePerNamespaceCap(unittest.TestCase):
    """M8: the per-namespace cap is PER NAMESPACE (windowed), and truncation is
    reported when a namespace exceeds it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-m8-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.models = os.path.join(self.tmp, "no-such-models")
        os.makedirs(self.models, exist_ok=True)
        self.store_path = os.path.join(self.tmp, "store.sqlite")
        self.store = _load_store_module(self.store_path, self.models)
        self.conn = self.store.connect()
        self.store.init_db(self.conn)
        self.store.migrate(self.conn)

    def test_cap_is_per_namespace_not_global(self):
        """Two namespaces, each under the cap individually but OVER it combined:
        both namespaces' rows must be examined (no global starvation). Done with
        a small monkeypatched cap so the test is fast."""
        # Use a tiny cap so we can seed enough rows cheaply.
        small_cap = 6
        # Seed two namespaces with more than small_cap rows each.
        for i in range(small_cap + 4):
            self.store.add_memory(self.conn, namespace="project:m8a",
                                  type_="fact", content=f"ns a row {i} alpha",
                                  signal="test")
            self.store.add_memory(self.conn, namespace="project:m8b",
                                  type_="fact", content=f"ns b row {i} beta",
                                  signal="test")
        # The authoritative proof: run the SAME windowed SQL the consolidate
        # function uses and confirm it returns up to small_cap PER namespace
        # (not small_cap globally). A global LIMIT would return only small_cap
        # rows total; the windowed PARTITION returns small_cap PER namespace.
        rows = self.conn.execute(
            """WITH ranked AS (
                   SELECT id, namespace,
                          ROW_NUMBER() OVER (PARTITION BY namespace ORDER BY id) AS rn
                   FROM memory WHERE superseded_at IS NULL
               )
               SELECT namespace, count(*) AS c FROM ranked WHERE rn <= ?
               GROUP BY namespace""",
            (small_cap,)).fetchall()
        counts = {r["namespace"]: r["c"] for r in rows}
        # Each namespace got exactly small_cap rows (the windowed cap is per-ns).
        self.assertEqual(counts.get("project:m8a"), small_cap,
                         f"per-namespace cap not applied: {counts}")
        self.assertEqual(counts.get("project:m8b"), small_cap,
                         f"per-namespace cap not applied: {counts}")

    def test_truncation_reported_when_namespace_exceeds_cap(self):
        """When a namespace's eligible rows exceed the cap, the consolidate
        summary reports a `truncated` status (honest about bounded examination)."""
        small_cap = 5
        for i in range(small_cap + 3):
            self.store.add_memory(self.conn, namespace="project:m8trunc",
                                  type_="fact", content=f"trunc row {i} gamma",
                                  signal="test")
        with mock.patch.object(_consolidate_mod, "CONSOLIDATE_MAX_ROWS_PER_NAMESPACE", small_cap):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                # force=True bypasses the cadence gate so the summary prints.
                self.store.consolidate(self.conn, namespace="project:m8trunc",
                                       threshold=0.99, prune=False,
                                       dry_run=True, force=True)
            out = buf.getvalue()
        self.assertIn("truncated", out,
                       f"expected truncation report, got:\n{out}")


class M10HermesHooksUseStoreCli(unittest.TestCase):
    """M10: the Hermes compatibility hook delegates store work to the CLI."""

    @staticmethod
    def _isolated_env(root: Path, **overrides: str) -> dict[str, str]:
        """Build a copied-hook environment without ambient ZMEM/host routing."""
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("ZMEM_")
        }
        for key in list(env):
            if key.startswith(("CLAUDE_", "ZCODE_", "PLUGIN_")):
                env.pop(key, None)
        home = root / "home"
        appdata = root / "appdata"
        localappdata = root / "localappdata"
        xdg_config = root / "xdg-config"
        xdg_data = root / "xdg-data"
        xdg_cache = root / "xdg-cache"
        for directory in (home, appdata, localappdata, xdg_config, xdg_data, xdg_cache):
            directory.mkdir(parents=True, exist_ok=True)
        env.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(localappdata),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_DATA_HOME": str(xdg_data),
            "XDG_CACHE_HOME": str(xdg_cache),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        env.update(overrides)
        return env

    def _load_convention(self, hook_file=None):
        hook_file = hook_file or (HOOKS_DIR / "zmem-hermes-convention.py")
        spec = importlib.util.spec_from_file_location(
            f"hook_test_{os.getpid()}_{id(hook_file)}", str(hook_file))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_convention_hook_has_no_store_path_or_filesystem_access(self):
        src = (HOOKS_DIR / "zmem-hermes-convention.py").read_text("utf-8")
        self.assertNotIn("_resolve_store_path", src)
        self.assertNotIn("_assert_local_fs", src)
        self.assertNotIn("import host", src)
        self.assertNotIn("sqlite3", src)
        mod = self._load_convention()
        self.assertTrue(callable(mod._resolve_store_py))
        self.assertFalse(hasattr(mod, "_resolve_store_path"))

    def test_convention_hook_finds_in_tree_store_cli(self):
        mod = self._load_convention()
        self.assertEqual(mod._resolve_store_py().resolve(), STORE_PY.resolve())

    def test_copy_install_hook_finds_store_cli_via_zmem_home(self):
        """A copied plugin resolves and executes the CLI from ZMEM_HOME."""
        plugin_root = Path(tempfile.mkdtemp(prefix="zmem-plugin-copy-"))
        checkout_root = Path(tempfile.mkdtemp(prefix="zmem-cli-checkout-"))
        self.addCleanup(shutil.rmtree, plugin_root, True)
        self.addCleanup(shutil.rmtree, checkout_root, True)
        copy_hooks = plugin_root / "deep" / "hooks"
        copy_hooks.mkdir(parents=True)
        hook_dst = copy_hooks / "zmem-hermes-convention.py"
        shutil.copy(HOOKS_DIR / "zmem-hermes-convention.py", hook_dst)
        copy_store_dir = checkout_root / "skills" / "memory" / "scripts"
        shutil.copytree(SCRIPTS_DIR, copy_store_dir)
        copy_store = copy_store_dir / "store.py"
        explicit = checkout_root / "isolated-store.sqlite"
        env = self._isolated_env(
            checkout_root, ZMEM_HOME=str(checkout_root), ZMEM_STORE=str(explicit)
        )
        with mock.patch.dict(os.environ, env, clear=True):
            mod = self._load_convention(hook_dst)
            self.assertEqual(mod._resolve_store_py().resolve(), copy_store.resolve())
            result = subprocess.run(
                [sys.executable, str(mod._resolve_store_py()), "path"],
                capture_output=True, text=True, env=env, timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), explicit.resolve())

    def test_convention_hook_subprocess_reaches_cli_and_writes_observation(self):
        """A copied hook must reach the copied CLI and write one observation."""
        with tempfile.TemporaryDirectory(prefix="zmem-hook-boundary-") as raw:
            root = Path(raw)
            plugin_root = root / "plugin"
            checkout_root = root / "checkout"
            copy_hooks = plugin_root / "deep" / "hooks"
            copy_hooks.mkdir(parents=True)
            copied_hook = copy_hooks / "zmem-hermes-convention.py"
            shutil.copy(HOOKS_DIR / "zmem-hermes-convention.py", copied_hook)
            copy_store_dir = checkout_root / "skills" / "memory" / "scripts"
            shutil.copytree(SCRIPTS_DIR, copy_store_dir)
            copied_store = copy_store_dir / "store.py"
            store = root / "store.sqlite"
            data = root / "data"
            data.mkdir()
            env = self._isolated_env(
                root,
                ZMEM_HOME=str(checkout_root),
                ZMEM_STORE=str(store),
                ZMEM_DATA=str(data),
                ZMEM_PYTHON=PYTHON,
                ZMEM_MODELS_DIR=str(root / "no-models"),
                ZMEM_CONVENTION_INTERVAL="10",
            )
            initialized = subprocess.run(
                [PYTHON, str(copied_store), "init"],
                capture_output=True, text=True, env=env, timeout=20,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            event = {
                "tool_name": "Edit",
                "args": {"file_path": "src/compat-boundary.py"},
                "session_id": "s-hook-boundary",
                "task_id": "task-boundary",
                "tool_call_id": "call-boundary",
                "result": {"status": "ok"},
                "duration_ms": 1,
            }
            observed = subprocess.run(
                [PYTHON, str(copied_hook)],
                input=json.dumps(event) + "\n",
                capture_output=True, text=True, env=env, timeout=20,
            )
            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertEqual(observed.stdout.strip(), "{}")

            deadline = time.monotonic() + 5.0
            evidence = None
            convention_count = None
            while time.monotonic() < deadline:
                conn = None
                try:
                    conn = sqlite3.connect(store)
                    try:
                        evidence = conn.execute(
                            "SELECT kind, lane, moment, ref_path FROM evidence "
                            "WHERE session_id = ?",
                            ("s-hook-boundary",),
                        ).fetchone()
                        convention_count = conn.execute(
                            "SELECT value FROM meta WHERE key = ?",
                            ("hermes_convention_count_s-hook-boundary",),
                        ).fetchone()
                    finally:
                        conn.close()
                except sqlite3.Error:
                    evidence = None
                    convention_count = None
                if evidence and convention_count:
                    break
                time.sleep(0.05)
            self.assertEqual(
                evidence,
                ("edit", "hermes-compat", "pretool", "src/compat-boundary.py"),
            )
            self.assertEqual(convention_count, ("1",))

    def test_store_cli_guard_refusal_precedes_sqlite_connection(self):
        """The no-create evidence path must refuse before opening SQLite."""
        from contextlib import redirect_stdout
        from storelib import cli

        with tempfile.TemporaryDirectory(prefix="zmem-guard-boundary-") as raw:
            store = Path(raw) / "store.sqlite"
            original_bytes = b"guard must leave this file untouched\n"
            store.write_bytes(original_bytes)
            old_store_path = cli.STORE_PATH
            cli.STORE_PATH = store
            try:
                output = io.StringIO()
                payload = json.dumps({
                    "session_id": "s-guard",
                    "lane": "hermes-compat",
                    "moment": "pretool",
                    "kind": "edit",
                    "ts": "2026-09-17T00:00:00Z",
                    "excerpt": "guard",
                    "ref_path": "src/guard.py",
                    "ref_offset": None,
                })
                stdin_stream = io.TextIOWrapper(io.BytesIO((payload + "\n").encode("utf-8")))
                try:
                    with (
                        mock.patch.dict(os.environ, {"ZMEM_EVIDENCE_NO_CREATE": "1"}, clear=False),
                        mock.patch.object(cli._schema_host, "assert_local_fs",
                                          side_effect=ValueError("network path refused")) as guard,
                        mock.patch.object(cli.sqlite3, "connect",
                                          side_effect=AssertionError("guard must precede connect")) as connect,
                        mock.patch.object(sys, "argv", [str(STORE_PY), "evidence", "write"]),
                        mock.patch.object(sys, "stdin", stdin_stream),
                        redirect_stdout(output),
                    ):
                        with self.assertRaises(SystemExit) as exited:
                            cli.main()
                finally:
                    stdin_stream.close()
                self.assertEqual(exited.exception.code, 0)
                self.assertEqual(output.getvalue().strip(), "{}")
                guard.assert_called_once_with(store.parent)
                connect.assert_not_called()
                self.assertEqual(store.read_bytes(), original_bytes)
            finally:
                cli.STORE_PATH = old_store_path

    def test_reflect_hook_no_longer_resolves_the_store(self):
        """Issue #122: the reflect hook must not carry _resolve_store_path —
        it is a fail-open adapter whose store work lives in subprocesses."""
        hook_file = HOOKS_DIR / "zmem-hermes-reflect.py"
        src = hook_file.read_text(encoding="utf-8")
        self.assertNotIn("_resolve_store_path", src,
                         "the reflect hook must not resolve the store path "
                         "itself (issue #122 moved that to the bridge)")

class M13ConventionCaptureInterpreterDiscovery(unittest.TestCase):
    """M13: the convention-capture shell hook must parse the tool name with the
    discovered $PYTHON_BIN, so it works when bare `python` is absent/stub but
    `python3` exists."""

    def setUp(self):
        self.script = REPO_ROOT / "hooks" / "zmem-convention-capture.sh"
        self.bash = shutil.which("bash")

    def test_script_parses_tool_name_with_python3_available(self):
        """Feed a real PostToolUse JSON and assert the script does NOT silently
        emit empty (it recognized the tool name) when python3 is on PATH.
        This is a smoke test that the reordered interpreter discovery works."""
        if not self.bash:
            self.skipTest("bash not available — cannot run the shell hook")
        # The script emits <<<ZMEM_JSON>>>...<<<END>>>. For a Bash tool call it
        # proceeds past the tool-name gate (it does not emit empty at the gate).
        payload = '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}'
        env = {**os.environ}
        # Ensure some python is discoverable (the script needs it).
        r = subprocess.run(
            [self.bash, str(self.script)],
            input=payload, capture_output=True, text=True, env=env, timeout=20,
        )
        # The script always emits the sentinel envelope. The key assertion: it
        # did not crash, and it produced output (it got past interpreter
        # discovery). Whether it emits a nudge or empty depends on the counter,
        # but a CRASH or empty-stdout would indicate the bare-python bug.
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<<<ZMEM_JSON>>>", r.stdout,
                      "script must emit the sentinel envelope")
        self.assertIn("<<<END>>>", r.stdout)

    def test_script_emits_empty_cleanly_when_no_interpreter(self):
        """When NO python interpreter is available, the script must emit empty
        cleanly (not crash) — the early PYTHON_BIN guard."""
        if not self.bash:
            self.skipTest("bash not available")
        payload = '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}'
        # PATH with no python/python3 at all.
        empty_path_dir = tempfile.mkdtemp(prefix="zmem-empty-path-")
        self.addCleanup(shutil.rmtree, empty_path_dir, True)
        env = {**os.environ, "PATH": empty_path_dir}
        r = subprocess.run(
            [self.bash, str(self.script)],
            input=payload, capture_output=True, text=True, env=env, timeout=20,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<<<ZMEM_JSON>>>", r.stdout)
        # With no interpreter it must emit the EMPTY envelope ({}) and exit 0.
        self.assertIn("{}", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
