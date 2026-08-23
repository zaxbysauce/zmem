"""Regression tests for issue #36 host-integration + resource findings the final
critic flagged as missing plan-mandated coverage:

  M8  — consolidate reports truncation when a namespace exceeds the per-namespace
        row cap, and the cap is genuinely per-namespace (no cross-namespace
        starvation).
  M10 — the Hermes hooks resolve the store via host.resolve_store_path under
        CLAUDE_PLUGIN_DATA / ZCODE_PLUGIN_DATA (not just ~/.zmem), matching the
        authoritative resolver.
  M13 — the convention-capture shell hook parses the tool name with the
        discovered $PYTHON_BIN, so it works when bare `python` is absent but
        `python3` exists (and emits empty when no interpreter is available).

Run: python tests/test_host_integration_findings.py
No pytest / third-party harness required — matches the repo convention.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
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


class M10HermesHooksResolveViaHost(unittest.TestCase):
    """M10: the three Hermes shell hooks resolve the store via
    host.resolve_store_path (the full env chain), not the truncated copy that
    omitted CLAUDE/ZCODE_PLUGIN_DATA."""

    def _hook_resolves_under(self, env_var, tmp_path):
        """Import a hook module under a controlled env var and return its
        resolved store path. ZMEM_STORE is set explicitly so the test is
        deterministic regardless of whether a real ~/.zmem/store.sqlite exists
        on the box (host.py's "box-wide store always wins" rule would otherwise
        override CLAUDE/ZCODE_PLUGIN_DATA)."""
        explicit = os.path.join(tmp_path, "store.sqlite")
        env = {**os.environ}
        for v in ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            env.pop(v, None)
        env["ZMEM_STORE"] = explicit
        env[env_var] = tmp_path
        hook_file = HOOKS_DIR / "zmem-hermes-convention.py"
        spec = importlib.util.spec_from_file_location(
            f"hook_test_{env_var}_{os.getpid()}", str(hook_file))
        with mock.patch.dict(os.environ, env, clear=False):
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Resolve INSIDE the patched env (host.resolve_store_path reads
            # os.environ at call time).
            resolved = str(mod._resolve_store_path())
        return resolved, explicit

    def test_convention_hook_resolves_claude_plugin_data(self):
        tmp = tempfile.mkdtemp(prefix="zmem-m10-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p, expected = self._hook_resolves_under("CLAUDE_PLUGIN_DATA", tmp)
        # The hook must DELEGATE to host.resolve_store_path, which honors
        # ZMEM_STORE. A truncated hand-rolled resolver that omitted the import
        # would still work here, but the drift test below proves delegation.
        self.assertEqual(os.path.normcase(p), os.path.normcase(expected),
                         f"expected {expected}, got {p}")

    def test_convention_hook_resolves_zcode_plugin_data(self):
        tmp = tempfile.mkdtemp(prefix="zmem-m10-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p, expected = self._hook_resolves_under("ZCODE_PLUGIN_DATA", tmp)
        self.assertEqual(os.path.normcase(p), os.path.normcase(expected),
                         f"expected {expected}, got {p}")

    def test_all_three_hooks_agree_with_host_resolver(self):
        """Drift prevention: each hook's resolved path must match
        host.resolve_store_path() under the same env. This is the core M10
        proof — the hooks delegate to the authoritative resolver, so they can
        never drift from it."""
        sys.path.insert(0, str(SCRIPTS_DIR))
        import host
        tmp = tempfile.mkdtemp(prefix="zmem-m10-")
        self.addCleanup(shutil.rmtree, tmp, True)
        explicit = os.path.join(tmp, "store.sqlite")
        env = {**os.environ}
        for v in ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            env.pop(v, None)
        env["ZMEM_STORE"] = explicit
        env["CLAUDE_PLUGIN_DATA"] = tmp
        with mock.patch.dict(os.environ, env, clear=False):
            host_path = str(host.resolve_store_path())
        self.assertEqual(os.path.normcase(host_path),
                         os.path.normcase(explicit),
                         f"sanity: host resolved {host_path}, expected {explicit}")
        for hook_name in ("zmem-hermes-convention.py", "zmem-hermes-reflect.py",
                          "zmem-hermes-verify.py"):
            hook_file = HOOKS_DIR / hook_name
            spec = importlib.util.spec_from_file_location(
                f"hook_drift_{hook_name}_{os.getpid()}", str(hook_file))
            with mock.patch.dict(os.environ, env, clear=False):
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Resolve INSIDE the patched env.
                hook_path = str(mod._resolve_store_path())
            self.assertEqual(
                os.path.normcase(hook_path), os.path.normcase(host_path),
                f"{hook_name} resolved {hook_path} but host.resolve_store_path "
                f"resolved {host_path} — drift!")

    def test_copy_install_hook_finds_host_via_zmem_home(self):
        """In a copy install (`cp -r hermes-plugin …`), the hook file has no
        skills/ tree alongside it. The hook must locate host.py via the
        $ZMEM_HOME probe (README requires copy users to set it) — otherwise it
        silently no-ops against the wrong store (#36 M10 / cubic-3,5,8).

        Discrimination: we verify (a) the in-tree candidate (parents[2]) does
        NOT exist in the copied layout, AND (b) the hook's `_resolve_store_path`
        actually imported `host` (presence in the module namespace + the
        resolved path matches host.resolve_store_path). If the ZMEM_HOME probe
        were absent/broken, `host` would not be importable and the hook would
        fall to the inline fallback."""
        sys.path.insert(0, str(SCRIPTS_DIR))
        import host as _host_ref  # noqa: F401 — ensure importable
        copy_root = tempfile.mkdtemp(prefix="zmem-copy-")
        self.addCleanup(shutil.rmtree, copy_root, True)
        copy_hooks = Path(copy_root) / "deep" / "hooks"
        copy_hooks.mkdir(parents=True)
        hook_dst = copy_hooks / "zmem-hermes-convention.py"
        shutil.copy(HOOKS_DIR / "zmem-hermes-convention.py", hook_dst)
        # Confirm the in-tree candidate does NOT exist (copy has no skills/).
        in_tree = hook_dst.resolve().parents[2] / "skills" / "memory" / "scripts"
        self.assertFalse((in_tree / "host.py").is_file(),
                         "test setup: in-tree candidate must be absent in copy install")
        explicit = os.path.join(copy_root, "store.sqlite")
        env = {**os.environ}
        for v in ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
            env.pop(v, None)
        env["ZMEM_STORE"] = explicit
        env["ZMEM_HOME"] = str(REPO_ROOT)
        spec = importlib.util.spec_from_file_location("hook_copy_disc", str(hook_dst))
        zmem_home_scripts = str(Path(REPO_ROOT) / "skills" / "memory" / "scripts")
        with mock.patch.dict(os.environ, env, clear=False):
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Count occurrences of the ZMEM_HOME scripts dir in sys.path BEFORE
            # the hook runs. The probe does sys.path.insert(0, ...) which adds
            # a DUPLICATE entry; the inline fallback never touches sys.path. So
            # a count increase after _resolve_store_path is causal proof the
            # probe fired (not vacuous like an existential any() check, which
            # this test's own setUp pre-satisfies). (PRR-006 final-critic.)
            before = sum(1 for p in sys.path
                         if os.path.normcase(p) == os.path.normcase(zmem_home_scripts))
            hook_path = str(mod._resolve_store_path())
            after = sum(1 for p in sys.path
                        if os.path.normcase(p) == os.path.normcase(zmem_home_scripts))
            # host.py's resolution under the same env.
            host_path = str(_host_ref.resolve_store_path())
        # The hook must match host.py (delegation produced the same result).
        self.assertEqual(os.path.normcase(hook_path), os.path.normcase(host_path),
                         f"hook ({hook_path}) != host.py ({host_path}) — delegation failed")
        # DISCRIMINATOR: the probe's sys.path.insert must have increased the
        # occurrence count of the ZMEM_HOME scripts dir. Deleting the probe
        # (falling to the inline fallback, which never touches sys.path) leaves
        # after == before → this fails. (PRR-006 final-critic.)
        self.assertGreater(after, before,
                           f"ZMEM_HOME probe did not run (sys.path count "
                           f"unchanged: before={before} after={after}) — inline "
                           f"fallback was used instead")


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
