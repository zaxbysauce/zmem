"""PR2 native knowledge-policy wiring (tests/test_knowledge_policy_native.py).

Covers ONLY this PR's owned surfaces (docs/KNOWLEDGE-ROUTING.md):

* the Hermes provider's conditional client-side append in
  ``system_prompt_block`` (after the inject gate, cached, byte-identical
  when the companion is off), plus ONE real-helper Hermes leg driving
  the actual system-prompt block against the real policy helper from
  this checkout (skips only when that helper file is absent),
* the dsh adapter's ``runPolicy`` launcher seam (bounded, fail-open,
  frozen helper CLI contract, no config-path duplication),
* the dsh ``agent/pre-step`` once-per-session policy append (runtime legs
  — skipped honestly when the pinned dsh runtime is absent), and the dsh
  ``agent/disposed`` pending-capture drain (a direct plugin-import leg
  that runs wherever node+python exist — only the pinned dsh-llm bare
  import is stubbed through a node module-register hook, disclosed in the
  class docstring),
* the portable skills' and the routing doc's load-bearing truths.

FROZEN helper contract (the real
``skills/memory/scripts/knowledge/policy.py`` has landed; the launcher
legs below exercise it directly, the adapter legs drive a STUB):

    render_policy(config_path=None, environ=None, allow_capture=True) -> str
    CLI: --config PATH (optional) | --no-capture | --text | --json,
         exit 0 rendered-or-disabled, exit 2 usage (incl. a missing
         --config value)
    resolve_config_path(environ=None, *, home=None)  # the single config
        # authority (home is keyword-only — test isolation)

Adapters spawn the helper with NO ``--config`` (the helper resolves the
canonical store config from the resolver variables the adapter passes)
and map ``ZMEM_CAPTURE=0`` to ``--no-capture``; ``ZMEM_INJECT=0`` is the
adapter's own no-spawn gate. The adapter legs drive a STUB helper
implementing exactly that CLI so the ADAPTER behavior is pinned without
depending on the helper's renderer; the launcher seam and real-helper
legs exercise the delivered renderer end to end.

Default-off byte-identity is asserted by comparing outputs against the
pre-change shapes (core.md alone for Hermes; no non-user messages for dsh
against a silent backend).

Runs under pytest and standalone:
``python -X utf8 -m pytest --disable-plugin-autoload --noconftest -p
no:cacheprovider -q tests/test_knowledge_policy_native.py``
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_INIT = REPO_ROOT / "hermes-plugin" / "__init__.py"
LAUNCHER_MJS = REPO_ROOT / "deepseek-plugin" / "launcher.mjs"
ROUTING_DOC = REPO_ROOT / "docs" / "KNOWLEDGE-ROUTING.md"
KNOWLEDGE_SKILL = REPO_ROOT / "skills" / "knowledge" / "SKILL.md"
CLOSEOUT_SKILL = REPO_ROOT / "skills" / "closeout" / "SKILL.md"
CLOSEOUT_REMOTE_SKILL = REPO_ROOT / "skills" / "closeout-remote" / "SKILL.md"

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
try:
    import deepseek_harness as harness  # noqa: E402
except Exception:  # pragma: no cover — harness always ships with the repo
    harness = None

# Short enough to survive the scenario driver's 80-char text80 projection.
POLICY_TEXT = "KNOWLEDGE-POLICY-STUB targets=notes,fleet capture=explicit-only"
POLICY_TEXT_NO_CAPTURE = (
    "KNOWLEDGE-POLICY-STUB targets=notes,fleet read-only no-capture-invite")
CORE_TEXT = "Tier0 core rules: hermetic fixture core.md body."

# The STUB helper — implements the frozen CLI, records every invocation to
# a marker JSONL file, and misbehaves on demand (ZMEM_TEST_POLICY_MODE).
STUB_POLICY_PY = '''\
import json, os, sys, time
mode = os.environ.get("ZMEM_TEST_POLICY_MODE", "enabled")
marker = os.environ.get("ZMEM_TEST_POLICY_MARKER", "")
if marker:
    with open(marker, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "flags": [a for a in sys.argv[1:]],
            "store": os.environ.get("ZMEM_STORE", ""),
            "data": os.environ.get("ZMEM_DATA", ""),
            "inject": os.environ.get("ZMEM_INJECT", ""),
            "capture": os.environ.get("ZMEM_CAPTURE", ""),
        }) + "\\n")
if mode == "hang":
    time.sleep(30)
if mode == "fail":
    sys.exit(3)
if mode == "disabled":
    sys.exit(0)
if "--no-capture" in sys.argv[1:]:
    print(%r)
else:
    print(%r)
''' % (POLICY_TEXT_NO_CAPTURE, POLICY_TEXT)

# Env-probe helper: records its FULL child environment to the marker (a
# sentinel-LEAK probe, FB-028) and prints the enabled stub text.
ENV_PROBE_POLICY_PY = '''\
import json, os, sys
marker = os.environ.get("ZMEM_TEST_POLICY_MARKER", "")
if marker:
    with open(marker, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"flags": [a for a in sys.argv[1:]],
                             "env": dict(os.environ)}) + "\\n")
print(%r)
''' % (POLICY_TEXT,)

REAL_POLICY_PY = (REPO_ROOT / "skills" / "memory" / "scripts"
                  / "knowledge" / "policy.py")

# Environment keys the hermes tests pin or strip (the t13/hermes discipline:
# ambient plugin-data vars silently change path resolution).
_PIN_KEYS = ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_INJECT",
             "ZMEM_CAPTURE", "ZMEM_CORE_MD", "ZMEM_REMOTE_URL",
             "ZMEM_MCP_URL", "ZMEM_NAMESPACE", "ZMEM_MCP_TOKEN",
             "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
             "ZCODE_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA",
             "ZMEM_TEST_POLICY_MARKER", "ZMEM_TEST_POLICY_MODE")


class _Env:
    """Snapshot/pin/restore the adapter-visible environment."""

    def __init__(self, **pins):
        self.pins = {k: v for k, v in pins.items() if v is not None}
        self.pop = [k for k in _PIN_KEYS if k not in self.pins]
        self._saved = {}

    def __enter__(self):
        for key in _PIN_KEYS:
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        os.environ.update(self.pins)
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def _read_marker(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# CI-003-R1 test-integrity repair (round-3 cluster 5): the hermes execs
# below install agent-module stubs (and, as an exec side effect, the
# plugin registers zmem_hermes_transport) that previously leaked for the
# rest of the session — tearDownClass only rmtree'd. Exact-restore
# discipline, the tests/test_knowledge_hub_integration.py pattern:
# snapshot the managed names BEFORE stubbing, then in teardown delete
# EVERY current match and put the exact previous objects back (names
# that were previously absent stay removed).
def _managed_module(key: str) -> bool:
    return (key == "agent" or key.startswith("agent.")
            or key.startswith("zmem_hermes"))


def _install_agent_stubs() -> dict:
    """Install the minimal agent.memory_provider ABC stubs the hermes
    plugin needs at import time; return the exact-restore snapshot."""
    saved = {key: value for key, value in list(sys.modules.items())
             if _managed_module(key)}
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # noqa: D401 — minimal ABC stub
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules["agent"] = agent
    sys.modules["agent.memory_provider"] = mp
    return saved


def _restore_stub_modules(saved: dict) -> None:
    for key in [key for key in list(sys.modules)
                if _managed_module(key)]:
        del sys.modules[key]
    sys.modules.update(saved)


class HermesPolicyAppendTest(unittest.TestCase):
    """system_prompt_block: conditional append, gates, byte-identity."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="zmem-pr2-hermes-"))
        # A scratch "zmem home" carrying ONLY the frozen helper (the stub)
        # and a marker store.py — enough for _resolve_zmem_home(); the
        # provider falls back to its dependency-free store-path chain, and
        # the helper (not the provider) owns config resolution anyway.
        scripts = cls._tmp / "home" / "skills" / "memory" / "scripts"
        (scripts / "knowledge").mkdir(parents=True)
        (scripts / "store.py").write_text("raise SystemExit(0)\n",
                                          encoding="utf-8")
        cls.policy_py = scripts / "knowledge" / "policy.py"
        cls.policy_py.write_text(STUB_POLICY_PY, encoding="utf-8")
        cls.data = cls._tmp / "data"
        cls.data.mkdir()
        (cls.data / "core.md").write_text(CORE_TEXT + "\n", encoding="utf-8")
        (cls.data / "config.json").write_text(
            json.dumps({"auto_retain": False}), encoding="utf-8")
        # Exact-restore stubs (CI-003-R1): _install_agent_stubs snapshots
        # the managed sys.modules names BEFORE stubbing; tearDownClass
        # restores the exact previous state via _restore_stub_modules.
        cls._saved_modules = _install_agent_stubs()
        with _Env(ZMEM_HOME=str(cls._tmp / "home"),
                  ZMEM_STORE=str(cls.data / "store.sqlite"),
                  ZMEM_DATA=str(cls.data),
                  ZMEM_MODEL_AUTODOWNLOAD="0",
                  ZMEM_MODELS_DIR=str(cls._tmp / "no-models")):
            spec = importlib.util.spec_from_file_location(
                "zmem_hermes_pr2_policy", HERMES_INIT)
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        _restore_stub_modules(cls._saved_modules)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _env(self, marker: Path, mode: str = "enabled", **extra):
        return _Env(
            ZMEM_HOME=str(self._tmp / "home"),
            ZMEM_STORE=str(self.data / "store.sqlite"),
            ZMEM_DATA=str(self.data),
            ZMEM_MODEL_AUTODOWNLOAD="0",
            ZMEM_MODELS_DIR=str(self._tmp / "no-models"),
            ZMEM_TEST_POLICY_MARKER=str(marker),
            ZMEM_TEST_POLICY_MODE=mode,
            **extra)

    def _block(self, marker: Path, mode: str = "enabled", **extra) -> str:
        with self._env(marker, mode, **extra):
            provider = self.mod.ZmemMemoryProvider()
            return provider.system_prompt_block()

    def test_default_off_disabled_helper_byte_identical(self):
        """Companion disabled → the block is EXACTLY core.md (old shape)."""
        marker = self._tmp / "m-disabled.jsonl"
        with self._env(marker, mode="disabled"):
            provider = self.mod.ZmemMemoryProvider()
            self.assertEqual(provider.system_prompt_block(), CORE_TEXT)
            # Cached: repeated calls must not respawn the helper.
            self.assertEqual(provider.system_prompt_block(), CORE_TEXT)
            self.assertEqual(provider.system_prompt_block(), CORE_TEXT)
        self.assertEqual(len(_read_marker(marker)), 1,
                         "one bounded probe per provider, not per call")

    def test_helper_absent_byte_identical_and_no_spawn(self):
        """No helper in the checkout → core.md only, helper never run."""
        marker = self._tmp / "m-absent.jsonl"
        absent_home = self._tmp / "home-nohelper"
        shutil.copytree(self._tmp / "home", absent_home)
        (absent_home / "skills" / "memory" / "scripts" / "knowledge"
         / "policy.py").unlink()
        with _Env(ZMEM_HOME=str(absent_home),
                  ZMEM_STORE=str(self.data / "store.sqlite"),
                  ZMEM_DATA=str(self.data),
                  ZMEM_MODEL_AUTODOWNLOAD="0",
                  ZMEM_MODELS_DIR=str(self._tmp / "no-models"),
                  ZMEM_TEST_POLICY_MARKER=str(marker),
                  ZMEM_TEST_POLICY_MODE="enabled"):
            provider = self.mod.ZmemMemoryProvider()
            self.assertEqual(provider.system_prompt_block(), CORE_TEXT)
        self.assertEqual(_read_marker(marker), [],
                         "an absent helper must never be spawned")

    def test_enabled_appends_after_core_with_frozen_flags(self):
        marker = self._tmp / "m-enabled.jsonl"
        out = self._block(marker, mode="enabled")
        self.assertEqual(out, CORE_TEXT + "\n\n" + POLICY_TEXT)
        rows = _read_marker(marker)
        self.assertEqual([r["flags"] for r in rows], [["--text"]],
                         "the adapter passes ONLY --text — the helper owns "
                         "config resolution (no --config, no paths, no "
                         "secrets in argv)")
        self.assertTrue(rows and rows[0]["store"].startswith(str(self._tmp)),
                        "the child env carried the user's own store flag")

    def test_inject_zero_no_output_and_no_spawn(self):
        marker = self._tmp / "m-inject.jsonl"
        out = self._block(marker, mode="enabled", ZMEM_INJECT="0")
        self.assertEqual(out, "")
        self.assertEqual(_read_marker(marker), [],
                         "ZMEM_INJECT=0 gates BEFORE any helper spawn")

    def test_capture_zero_still_appends_read_routing(self):
        """CAPTURE=0 does not silence the policy; the adapter forwards
        --no-capture and the helper renders the invitation-free text."""
        marker = self._tmp / "m-capture.jsonl"
        out = self._block(marker, mode="enabled", ZMEM_CAPTURE="0")
        self.assertEqual(out, CORE_TEXT + "\n\n" + POLICY_TEXT_NO_CAPTURE)
        rows = _read_marker(marker)
        self.assertEqual([r["flags"] for r in rows], [["--text",
                                                       "--no-capture"]])

    def test_capture_mode_change_respawns_once(self):
        marker = self._tmp / "m-modes.jsonl"
        with self._env(marker, mode="enabled"):
            provider = self.mod.ZmemMemoryProvider()
            self.assertIn(POLICY_TEXT, provider.system_prompt_block())
            os.environ["ZMEM_CAPTURE"] = "0"
            self.assertIn(POLICY_TEXT_NO_CAPTURE,
                          provider.system_prompt_block())
        rows = _read_marker(marker)
        self.assertEqual([r["flags"] for r in rows],
                         [["--text"], ["--text", "--no-capture"]])

    def test_helper_failure_fails_open(self):
        marker = self._tmp / "m-fail.jsonl"
        out = self._block(marker, mode="fail")
        self.assertEqual(out, CORE_TEXT)

    def test_helper_timeout_fails_open_bounded(self):
        marker = self._tmp / "m-hang.jsonl"
        started = time.monotonic()
        out = self._block(marker, mode="hang")
        elapsed = time.monotonic() - started
        self.assertEqual(out, CORE_TEXT)
        self.assertLess(elapsed, 25.0,
                        f"the policy spawn must stay hard-bounded "
                        f"({elapsed:.1f}s)")

    def test_no_core_md_policy_only(self):
        no_core = self._tmp / "data-nocore"
        no_core.mkdir(exist_ok=True)
        shutil.copy(self.data / "config.json", no_core / "config.json")
        marker = self._tmp / "m-nocore.jsonl"
        with _Env(ZMEM_HOME=str(self._tmp / "home"),
                  ZMEM_STORE=str(no_core / "store.sqlite"),
                  ZMEM_DATA=str(no_core),
                  ZMEM_MODEL_AUTODOWNLOAD="0",
                  ZMEM_MODELS_DIR=str(self._tmp / "no-models"),
                  ZMEM_TEST_POLICY_MARKER=str(marker),
                  ZMEM_TEST_POLICY_MODE="enabled"):
            provider = self.mod.ZmemMemoryProvider()
            self.assertEqual(provider.system_prompt_block(), POLICY_TEXT)


class HermesRealHelperPolicyTest(unittest.TestCase):
    """CI-003-R1 real-helper Hermes leg: the stub legs above pin the
    ADAPTER's behavior with a stub helper; this leg invokes the ACTUAL
    hermes-plugin system_prompt_block against the REAL
    skills/memory/scripts/knowledge/policy.py from THIS checkout (copied
    into a scratch home so the provider's store.py probe stays on the
    SystemExit marker — no real checkout side effects). Self-skips ONLY
    when the real helper file is absent from the checkout; CI has the
    repo, so the leg runs there (no unconditional skip)."""

    @classmethod
    def setUpClass(cls):
        if not REAL_POLICY_PY.is_file():
            raise unittest.SkipTest(
                "real policy helper not landed in this checkout")
        cls._tmp = Path(tempfile.mkdtemp(prefix="zmem-pr2-hermes-real-"))
        scripts = cls._tmp / "home" / "skills" / "memory" / "scripts"
        (scripts / "knowledge").mkdir(parents=True)
        (scripts / "store.py").write_text("raise SystemExit(0)\n",
                                          encoding="utf-8")
        cls.policy_py = scripts / "knowledge" / "policy.py"
        shutil.copyfile(REAL_POLICY_PY, cls.policy_py)
        cls.data = cls._tmp / "data"
        cls.data.mkdir()
        (cls.data / "core.md").write_text(CORE_TEXT + "\n", encoding="utf-8")
        # An ENABLED knowledge config beside the scratch store: the real
        # helper resolves it from the resolver env the provider's child
        # inherits (no --config — the frozen adapter spawn shape), so the
        # renderer has a real target roster to render.
        vault = cls._tmp / "vault"
        vault.mkdir()
        (cls.data / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(vault)}}}}),
            encoding="utf-8")
        cls._saved_modules = _install_agent_stubs()
        with _Env(ZMEM_HOME=str(cls._tmp / "home"),
                  ZMEM_STORE=str(cls.data / "store.sqlite"),
                  ZMEM_DATA=str(cls.data),
                  ZMEM_MODEL_AUTODOWNLOAD="0",
                  ZMEM_MODELS_DIR=str(cls._tmp / "no-models")):
            spec = importlib.util.spec_from_file_location(
                "zmem_hermes_pr2_policy_real", HERMES_INIT)
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        _restore_stub_modules(cls._saved_modules)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _pins(self) -> dict:
        return dict(ZMEM_HOME=str(self._tmp / "home"),
                    ZMEM_STORE=str(self.data / "store.sqlite"),
                    ZMEM_DATA=str(self.data),
                    ZMEM_MODEL_AUTODOWNLOAD="0",
                    ZMEM_MODELS_DIR=str(self._tmp / "no-models"))

    def test_system_prompt_block_appends_real_helper_policy(self):
        """Byte parity: the delivered block is EXACTLY core.md plus the
        REAL helper's own --text rendering under the same resolver env —
        the adapter appends what the delivered renderer prints, nothing
        else (the provider spawns sys.executable, so the comparison leg
        runs the same interpreter against the same copied helper)."""
        with _Env(**self._pins()):
            provider = self.mod.ZmemMemoryProvider()
            block = provider.system_prompt_block()
            helper = subprocess.run(
                [sys.executable, "-X", "utf8", str(self.policy_py),
                 "--text"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30)
        self.assertEqual(helper.returncode, 0,
                         f"real helper CLI failed: {helper.stderr!r}")
        rendered = (helper.stdout or "").strip()
        self.assertTrue(rendered,
                        "the real helper must render the enabled "
                        "companion's policy")
        self.assertIn("notes", rendered,
                      "the real rendering names the configured target id")
        self.assertEqual(block, CORE_TEXT + "\n\n" + rendered,
                         "the hermes block must be core.md plus exactly "
                         "the real helper's rendered policy")


class _NodeLeg:
    """Shared helpers for the node-subprocess legs."""

    @staticmethod
    def python_on_path():
        return shutil.which("python")

    @staticmethod
    def node_on_path():
        return shutil.which("node")

    @staticmethod
    def stub_tree(tmp: Path, with_helper: bool = True) -> Path:
        root = tmp / "root"
        scripts = root / "skills" / "memory" / "scripts"
        (scripts / "knowledge").mkdir(parents=True, exist_ok=True)
        (scripts / "store.py").write_text("raise SystemExit(0)\n",
                                          encoding="utf-8")
        if with_helper:
            (scripts / "knowledge" / "policy.py").write_text(
                STUB_POLICY_PY, encoding="utf-8")
        return root

    @staticmethod
    def run_node(script: str, env: dict, timeout_s: float = 60.0) -> dict:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env,
            cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
            timeout=timeout_s)
        assert proc.returncode == 0, (
            f"node leg failed rc={proc.returncode}: {proc.stderr[-600:]!r}")
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return json.loads(lines[-1])


_RUNPOLICY_PROBE = """
const mod = await import(process.env.LAUNCHER_URL);
const r = await mod.runPolicy({
  timeoutMs: Number(process.env.PROBE_TIMEOUT_MS || 5000),
});
console.log(JSON.stringify(r));
"""


class DshLauncherPolicyTest(_NodeLeg, unittest.TestCase):
    """runPolicy: frozen CLI flags, fail-open, bounded, env passthrough."""

    def setUp(self):
        if not (self.node_on_path() and self.python_on_path()):
            self.skipTest("node/python not on PATH for the launcher legs")
        self._tmp = Path(tempfile.mkdtemp(prefix="zmem-pr2-launcher-"))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _env(self, root: Path, marker: Path, mode: str,
             timeout_ms: int = 5000, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")
               and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}
        env.update({
            "LAUNCHER_URL": LAUNCHER_MJS.as_uri(),
            "ZMEM_ROOT": str(root),
            "ZMEM_STORE": str(root / "store.sqlite"),
            "PYTHONIOENCODING": "utf-8",
            "PROBE_TIMEOUT_MS": str(timeout_ms),
            "ZMEM_TEST_POLICY_MARKER": str(marker),
            "ZMEM_TEST_POLICY_MODE": mode,
        })
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def test_helper_absent_fail_open_without_spawn(self):
        root = self.stub_tree(self._tmp, with_helper=False)
        marker = self._tmp / "m.jsonl"
        out = self.run_node(
            _RUNPOLICY_PROBE, self._env(root, marker, "enabled"))
        self.assertEqual(out.get("text"), "")
        self.assertEqual(out.get("reason"), "helper-absent")
        self.assertEqual(_read_marker(marker), [])

    def test_disabled_renders_empty(self):
        root = self.stub_tree(self._tmp)
        marker = self._tmp / "m.jsonl"
        out = self.run_node(
            _RUNPOLICY_PROBE, self._env(root, marker, "disabled"))
        self.assertEqual(out.get("text"), "")
        self.assertEqual(out.get("reason"), "disabled")

    def test_enabled_renders_text_with_frozen_flags(self):
        root = self.stub_tree(self._tmp)
        marker = self._tmp / "m.jsonl"
        out = self.run_node(
            _RUNPOLICY_PROBE, self._env(root, marker, "enabled"))
        self.assertEqual(out.get("text"), POLICY_TEXT)
        self.assertEqual(out.get("reason"), "ok")
        rows = _read_marker(marker)
        self.assertEqual([r["flags"] for r in rows], [["--text"]],
                         "the launcher passes ONLY --text — config "
                         "resolution belongs to the helper")
        self.assertTrue(rows[0]["store"].startswith(str(self._tmp)),
                        "user store flag rode the child env")

    def test_capture_zero_passes_no_capture_flag(self):
        root = self.stub_tree(self._tmp)
        marker = self._tmp / "m.jsonl"
        out = self.run_node(
            _RUNPOLICY_PROBE,
            self._env(root, marker, "enabled", ZMEM_CAPTURE="0"))
        self.assertEqual(out.get("text"), POLICY_TEXT_NO_CAPTURE)
        rows = _read_marker(marker)
        self.assertEqual([r["flags"] for r in rows],
                         [["--text", "--no-capture"]])

    def test_helper_nonzero_exit_fails_open(self):
        root = self.stub_tree(self._tmp)
        marker = self._tmp / "m.jsonl"
        out = self.run_node(
            _RUNPOLICY_PROBE, self._env(root, marker, "fail"))
        self.assertEqual(out.get("text"), "")
        self.assertTrue(str(out.get("reason", "")).startswith("exit-"))

    def test_helper_hang_is_bounded(self):
        root = self.stub_tree(self._tmp)
        marker = self._tmp / "m.jsonl"
        started = time.monotonic()
        out = self.run_node(
            _RUNPOLICY_PROBE,
            self._env(root, marker, "hang", timeout_ms=700))
        elapsed = time.monotonic() - started
        self.assertEqual(out.get("text"), "")
        self.assertLess(elapsed, 20.0,
                        f"runPolicy must kill a wedged helper "
                        f"({elapsed:.1f}s)")

    def test_remote_mode_child_resolves_zmem_store_config(self):
        """FB-028: the store-lane normalization deletes ZMEM_STORE under
        ZMEM_REMOTE_URL — the POLICY child must keep it (its config
        resolution ranks ZMEM_STORE first; the delete exists for store
        access, not config resolution). Sentinel configs beside each
        candidate store prove the precedence: the store-side target id
        renders, the data-side one never appears."""
        root = self.stub_tree(self._tmp)
        shutil.copyfile(
            REAL_POLICY_PY,
            root / "skills" / "memory" / "scripts" / "knowledge"
            / "policy.py")
        store_side = self._tmp / "store-side"
        data_side = self._tmp / "data-side"
        for directory in (store_side, data_side):
            directory.mkdir()
        vault = str(self._tmp)  # never stat'ed by the display loader
        (store_side / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "store-side-wins": {"kind": "markdown", "root": vault}}}}),
            encoding="utf-8")
        (data_side / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "data-side-loses": {"kind": "markdown", "root": vault}}}}),
            encoding="utf-8")
        marker = self._tmp / "m.jsonl"
        out = self.run_node(_RUNPOLICY_PROBE, self._env(
            root, marker, "enabled",
            ZMEM_REMOTE_URL="https://hub.invalid.example/sentinel",
            ZMEM_STORE=str(store_side / "store.sqlite"),
            ZMEM_DATA=str(data_side)))
        self.assertEqual(out.get("reason"), "ok", f"leg: {out!r}")
        text = out.get("text") or ""
        self.assertIn("store-side-wins", text,
                      "remote mode must NOT strip ZMEM_STORE from the "
                      "policy child (config resolution, not store access)")
        self.assertNotIn("data-side-loses", text)

    def test_policy_child_env_is_minimal_no_sentinel_leak(self):
        """FB-028: only the documented resolver/system variables reach the
        spawned policy child — sentinel credential vars set in the parent
        env are absent from the child, while the resolver variables ride."""
        root = self.stub_tree(self._tmp)
        (root / "skills" / "memory" / "scripts" / "knowledge"
         / "policy.py").write_text(ENV_PROBE_POLICY_PY, encoding="utf-8")
        marker = self._tmp / "m.jsonl"
        out = self.run_node(_RUNPOLICY_PROBE, self._env(
            root, marker, "enabled",
            ZMEM_MCP_TOKEN="sentinel-mcp-token-not-a-secret",
            ZMEM_KB_FLEET_TOKEN="sentinel-kb-token-not-a-secret",
            ZMEM_PRINCIPAL="sentinel-principal",
            ZMEM_NAMESPACE="sentinel-namespace"))
        self.assertEqual(out.get("reason"), "ok", f"leg: {out!r}")
        rows = _read_marker(marker)
        self.assertEqual(len(rows), 1, "exactly one policy spawn")
        child_env = rows[0].get("env") or {}
        for leak in ("ZMEM_MCP_TOKEN", "ZMEM_KB_FLEET_TOKEN",
                     "ZMEM_PRINCIPAL", "ZMEM_NAMESPACE"):
            self.assertNotIn(leak, child_env,
                             f"credential/sentinel var {leak} leaked to "
                             f"the policy child")
        self.assertNotIn("sentinel-mcp-token-not-a-secret",
                         json.dumps(child_env))
        # Resolver variables DO ride the minimal child env.
        self.assertEqual(child_env.get("ZMEM_STORE"),
                         str(root / "store.sqlite"))
        self.assertTrue(child_env.get("PATH"),
                        "interpreter resolution needs PATH")


class DshPreStepPolicyTest(_NodeLeg, unittest.TestCase):
    """agent/pre-step once-per-session append, through the FROZEN scenario
    driver with a silent stub backend (runtime legs — skipped when the
    pinned dsh runtime is absent; reported as not performed)."""

    def setUp(self):
        if harness is None:
            self.skipTest("deepseek harness unavailable")
        try:
            harness.require_runtime()
        except harness.skip_exceptions() as exc:
            # The runtime legs skip honestly wherever the pinned dsh
            # runtime is absent (reported, never silently passed).
            self.skipTest(str(exc))
        if not (self.node_on_path() and self.python_on_path()):
            self.skipTest("node/python not on PATH for the scenario legs")

    def _scenario(self, scenario: str, root: Path, marker: Path, mode: str,
                  state: Path, **extra) -> dict:
        # ZMEM_ROOT points the LAUNCHER's backend at the scratch tree (stub
        # store.py + stub policy helper); ZMEM_DEEPSEEK_PLUGIN keeps the
        # frozen driver mounting the REAL repo plugin under edit.
        extra_env = {"ZMEM_ROOT": str(root),
                     "ZMEM_DEEPSEEK_PLUGIN": str(
                         REPO_ROOT / "deepseek-plugin" / "index.mjs"),
                     "ZMEM_STORE": str(root / "store.sqlite"),
                     "ZMEM_DATA": str(root / "data"),
                     "ZMEM_TEST_POLICY_MARKER": str(marker),
                     "ZMEM_TEST_POLICY_MODE": mode,
                     **extra}
        return harness.ok_or_red(
            harness.run_scenario(scenario, state, extra_env), scenario)

    def _policy_msgs(self, obs: dict) -> list:
        out = []
        for request in obs.get("requests") or []:
            for message in request.get("messages") or []:
                if message.get("sourceKind") == "zmem-policy":
                    out.append(message)
        return out

    def test_enabled_delivers_once_per_session(self):
        with harness.isolated_env("pr2-dsh-on") as tmp:
            root = _NodeLeg.stub_tree(tmp)
            (root / "data").mkdir(exist_ok=True)
            marker = tmp / "marker.jsonl"
            obs = self._scenario("timing_and_policy", root, marker,
                                 "enabled", tmp / "state.json")
            requests = obs.get("requests") or []
            self.assertGreaterEqual(len(requests), 2,
                                    "the scenario sends two requests")
            first = [m for m in requests[0]["messages"]
                     if m.get("sourceKind") == "zmem-policy"]
            self.assertEqual(len(first), 1,
                             "exactly one policy message on the first "
                             f"accepted pre-step: {requests[0]['messages']!r}")
            self.assertEqual(first[0].get("text80"), POLICY_TEXT)
            # Once per session: the durable session log carries that ONE
            # instance as history on later requests — a broken latch would
            # append a SECOND instance (two policy messages in one request).
            for index, request in enumerate(requests[1:]):
                count = sum(1 for m in request["messages"]
                            if m.get("sourceKind") == "zmem-policy")
                self.assertLessEqual(
                    count, 1,
                    f"request {index + 1} re-delivered the policy "
                    f"(once-per-session latch broken): {request['messages']!r}")
            state = obs.get("pluginState") or {}
            self.assertEqual(state.get("policyDeliveredText"), POLICY_TEXT)
            self.assertEqual(state.get("policyReason"), "ok")
            entries = state.get("sessions") or {}
            self.assertTrue(
                all(e.get("policyResolved") is True
                    and e.get("policyDelivered") is True
                    for e in entries.values() if isinstance(e, dict)),
                f"per-session latches must persist in native state: "
                f"{entries!r}")
            # The silent stub backend yields no recall message: the policy
            # append is INDEPENDENT of the recall envelope.
            self.assertFalse(any(
                m.get("sourceKind") == "zmem"
                for r in requests for m in r["messages"]))

    def test_default_off_no_policy_message_byte_identical_stream(self):
        with harness.isolated_env("pr2-dsh-off") as tmp:
            root = _NodeLeg.stub_tree(tmp)
            (root / "data").mkdir(exist_ok=True)
            marker = tmp / "marker.jsonl"
            obs = self._scenario("timing_and_policy", root, marker,
                                 "disabled", tmp / "state.json")
            requests = obs.get("requests") or []
            self.assertEqual(self._policy_msgs(obs), [],
                             "disabled companion appends NOTHING")
            # Byte-identical stream: the plugin adds NO message of its own
            # (only host-owned kinds remain; with the silent stub backend
            # there is no recall message either).
            added = {m.get("sourceKind") for r in requests
                     for m in r["messages"]} & {"zmem", "zmem-policy"}
            self.assertEqual(added, set(),
                             f"default-off must add no zmem-sourced "
                             f"messages ({added!r})")
            state = obs.get("pluginState") or {}
            self.assertEqual(state.get("policyDeliveredText"), "")
            self.assertEqual(state.get("policyReason"), "disabled")

    def test_inject_zero_no_append_no_spawn(self):
        with harness.isolated_env("pr2-dsh-inject") as tmp:
            root = _NodeLeg.stub_tree(tmp)
            (root / "data").mkdir(exist_ok=True)
            marker = tmp / "marker.jsonl"
            obs = self._scenario("timing_and_policy", root, marker,
                                 "enabled", tmp / "state.json",
                                 ZMEM_INJECT="0")
            self.assertEqual(self._policy_msgs(obs), [])
            # Under INJECT=0 every plugin handler is gated, so the state
            # file may never be written: the load-bearing claims are the
            # absent append and the absent helper spawn.
            state = obs.get("pluginState") or {}
            self.assertIn(state.get("policyReason"), (None, "none"))
            self.assertNotEqual(state.get("policyDeliveredText"), POLICY_TEXT)
            self.assertEqual(_read_marker(marker), [],
                             "INJECT=0 gates before any helper spawn")

    def test_capture_zero_policy_still_delivers_read_only(self):
        with harness.isolated_env("pr2-dsh-capture") as tmp:
            root = _NodeLeg.stub_tree(tmp)
            (root / "data").mkdir(exist_ok=True)
            marker = tmp / "marker.jsonl"
            obs = self._scenario("timing_and_policy", root, marker,
                                 "enabled", tmp / "state.json",
                                 ZMEM_CAPTURE="0")
            # One delivered instance; later requests carry it as history
            # (count per request, never a second instance).
            requests = obs.get("requests") or []
            counts = [sum(1 for m in r["messages"]
                          if m.get("sourceKind") == "zmem-policy")
                      for r in requests]
            self.assertEqual(counts[0], 1,
                             "CAPTURE=0 must NOT suppress the policy "
                             f"(counts={counts})")
            self.assertTrue(all(c <= 1 for c in counts),
                            f"once-per-session must hold under CAPTURE=0 "
                            f"(counts={counts})")
            delivered = [m for m in requests[0]["messages"]
                         if m.get("sourceKind") == "zmem-policy"][0]
            self.assertEqual(delivered.get("text80"), POLICY_TEXT_NO_CAPTURE)
            rows = _read_marker(marker)
            self.assertEqual([r["flags"] for r in rows],
                             [["--text", "--no-capture"]])

    def test_rejected_pre_step_never_receives_policy(self):
        with harness.isolated_env("pr2-dsh-reject") as tmp:
            root = _NodeLeg.stub_tree(tmp)
            (root / "data").mkdir(exist_ok=True)
            marker = tmp / "marker.jsonl"
            obs = self._scenario("first_request_delivery", root, marker,
                                 "enabled", tmp / "state.json",
                                 SCENARIO_REJECT="1")
            rejected = obs.get("rejected") or {}
            self.assertEqual(rejected.get("requestCountAfter"),
                             rejected.get("requestCountBefore"),
                             "rejected input never reaches the model")
            admitted = [m for m in self._policy_msgs(obs)]
            # The REJECTED agent's pre-step decision never entered the
            # gate: its session must not consume the once-per-session
            # latch through a delivered message it never had.
            # CI-003-R1 test-integrity repair: assert the rejected
            # session's native entry EXISTS unconditionally, then inspect
            # it — the old `if sid == "ds-rejected-input":` guard inside
            # the iteration was vacuously green whenever the producer
            # dropped the key. agent/created keeps the per-session latch
            # bookkeeping even when the pre-step decision is rejected.
            state = obs.get("pluginState") or {}
            sessions = state.get("sessions") or {}
            entry = sessions.get("ds-rejected-input")
            self.assertIsInstance(
                entry, dict,
                "the rejected session must keep a native state entry "
                f"(sessions={sessions!r}) — a dropped entry would hide "
                "whether the once-per-session latch was consumed")
            self.assertNotEqual(entry.get("policyDelivered"), True,
                                "a rejected decision must not count as a "
                                "policy delivery")
            self.assertNotEqual(entry.get("policyResolved"), True,
                                "a rejected pre-step never resolves the "
                                "policy — the once-per-session latch is "
                                "not consumed")


# -- agent/disposed capture drain (the PR2 plugin's own pending-capture
# guard): the probe imports the REAL deepseek-plugin/index.mjs with ONLY
# the pinned dsh-llm import stubbed through a node module-register hook
# (the dispose lane never touches dsh message APIs), so the pin RUNS on
# every host with node+python instead of skipping with the runtime.  The
# stub capture entry SLEEPS, holding the capture in flight at dispose.
_STUB_CAPTURE_ENTRY_PY = '''\
import json, time
# Hold the capture in flight long enough to still be pending at dispose.
time.sleep(0.6)
print(json.dumps({"ok": True, "deduped": False, "text": "dispose-drain-probe"}))
'''

_STUB_DSH_LLM_MJS = '''\
export function createUserMessage(init) { return { id: "stub-msg", ...init }; }
'''

_RESOLVE_HOOK_MJS = '''\
export async function resolve(specifier, context, next) {
  if (specifier === "@deepseek-ai/dsh-llm") {
    return { url: process.env.DSH_LLM_STUB_URL, shortCircuit: true };
  }
  return next(specifier, context);
}
'''

_REGISTER_HOOK_MJS = '''\
import { register } from "node:module";
register(new URL("./stub-resolve.mjs", import.meta.url), import.meta.url);
'''

_DISPOSE_DRAIN_PROBE = """
import fs from "node:fs";
const handlers = {};
const ctx = { on: (name, fn) => { (handlers[name] ??= []).push(fn); } };
const mod = await import(process.env.PLUGIN_URL);
await mod.plugin(ctx);
// An eligible FINAL tool failure starts an in-flight capture against the
// SLEEPING stub capture entry (still pending at dispose time).
handlers["tools/result"][0](
  { agent: { id: "ds-dispose-drain" } },
  { isError: true, error: { message: "dispose drain probe: flange tool failed" } });
await handlers["agent/disposed"][0]();
const state = JSON.parse(
  fs.readFileSync(process.env.ZMEM_DEEPSEEK_STATE, "utf8"));
console.log(JSON.stringify({
  captureQueue: state.captureQueue || [],
  disposeDrained: state.disposeDrained ?? null,
  persistsAcrossDispose: state.persistsAcrossDispose === true,
}));
"""


class DshDisposeDrainTest(_NodeLeg, unittest.TestCase):
    """agent/disposed drains this process's PENDING captures (the same
    bounded Promise.allSettled drain agent/turn-stopping already uses)
    and discloses the drain in state: a pending in-flight capture is
    never silently dropped on dispose, and the pending state file is
    preserved. RED at base: the old dispose handler wrote state with an
    empty captureQueue while the capture was still in flight."""

    def setUp(self):
        if not (self.node_on_path() and self.python_on_path()):
            self.skipTest("node/python not on PATH for the dispose leg")
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-dispose-drain-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _file_url(self, path: Path) -> str:
        proc = subprocess.run(
            ["node", "-e",
             "console.log(require('url').pathToFileURL(%r).href)"
             % str(path)],
            capture_output=True, text=True, timeout=30, check=True)
        return proc.stdout.strip()

    def test_pending_capture_is_drained_and_disclosed_at_dispose(self):
        root = self.tmp / "root"
        for rel, text in (
            ("dshstub/index.mjs", _STUB_DSH_LLM_MJS),
            ("reg/stub-resolve.mjs", _RESOLVE_HOOK_MJS),
            ("reg/reghook.mjs", _REGISTER_HOOK_MJS),
            ("root/hooks/lib/zmem-capture-entry.py",
             _STUB_CAPTURE_ENTRY_PY),
        ):
            target = self.tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        (root / "skills" / "memory" / "scripts").mkdir(parents=True,
                                                       exist_ok=True)
        (root / "skills" / "memory" / "scripts" / "store.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8")
        state_path = self.tmp / "state.json"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")
               and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                             "NODE_OPTIONS")}
        env.update({
            "ZMEM_DEEPSEEK_STATE": str(state_path),
            "ZMEM_ROOT": str(root),
            "DSH_LLM_STUB_URL": self._file_url(
                self.tmp / "dshstub" / "index.mjs"),
            "PLUGIN_URL": self._file_url(
                REPO_ROOT / "deepseek-plugin" / "index.mjs"),
            "PYTHONIOENCODING": "utf-8",
            # The register hook redirects the pinned dsh-llm bare import
            # to the stub above (disclosed: the dispose lane under test
            # never calls its exports).
            "NODE_OPTIONS": "--import=" + self._file_url(
                self.tmp / "reg" / "reghook.mjs"),
        })
        observed = self.run_node(_DISPOSE_DRAIN_PROBE, env, timeout_s=60.0)
        self.assertTrue(state_path.is_file(),
                        "the pending state file is preserved on dispose")
        queue = observed.get("captureQueue") or []
        self.assertTrue(
            any(e.get("kind") == "failure"
                and "dispose drain probe" in str(e.get("text") or "")
                for e in queue),
            f"the in-flight capture must be DRAINED into captureQueue "
            f"before dispose finishes: {observed!r}")
        self.assertEqual(observed.get("disposeDrained"), 1,
                         "the drain is disclosed in state (disposeDrained)")
        self.assertIs(observed.get("persistsAcrossDispose"), True)


class DocsAndSkillsTruthTest(unittest.TestCase):
    """Load-bearing truths pinned in the routing doc and the skills.

    Prose needles are checked against WHITESPACE-NORMALIZED text
    (``' '.join(text.split())``) so a rule phrased identically but wrapped
    across lines by an editor still matches — a wrapped needle must never
    produce a false FAIL, and (for the absent-claim checks) a wrapped
    false claim must never slip through. Every needle below carries the
    FULL rule phrase, not a shortened fragment. Code/command needles are
    single-line fragments, which normalization preserves verbatim.
    """

    @staticmethod
    def _norm(path: Path) -> str:
        return " ".join(path.read_text(encoding="utf-8").split())

    def test_routing_doc_required_truths(self):
        text = self._norm(ROUTING_DOC)
        for needle in (
            "render_policy(config_path=None, environ=None, allow_capture=True)",
            "--no-capture",
            "ZMEM_INJECT=0",
            "byte-identical",
            "resolve_config_path",
            "capability_gap",
            "no app-config install tool",
            "non-equivalence",
            "never import",
            "storelib",
            "per adapter process",
        ):
            self.assertIn(needle, text, f"routing doc lost truth: {needle!r}")
        self.assertIn("OpenCode", text)
        self.assertIn("gap remains", text)

    def test_knowledge_skill_required_truths(self):
        text = self._norm(KNOWLEDGE_SKILL)
        for needle in (
            "explicit", "pending", "NOT success", "transcripts",
            "credentials", "resolve_namespace", "knowledge capture",
            "knowledge read", "knowledge check", "knowledge status",
            "knowledge flush", "Obsidian",
            # Reviewer fixes: standing closeout authorization (no magic
            # phrase, no per-note ask), full command shape, $P interpreter.
            "Authorization", "standing opt-in", "re-ask per note",
            "do not demand a magic phrase",
            "--target <target-id>", '--namespace "<namespace>"',
            '"$P" -c', "external public",
        ):
            self.assertIn(needle, text,
                          f"knowledge skill lost truth: {needle!r}")

    def test_closeout_skill_standing_closeout_authorization(self):
        text = self._norm(CLOSEOUT_SKILL)
        for needle in (
            "ACTUAL USER asked for this closeout in THIS session",
            "standing opt-in",
            "no per-note confirmation and no magic phrase needed",
            "ZMEM_CAPTURE=0", "--target <target-id>",
            # Unavailable status is NOT silent loss (coordinator fix):
            # disabled stays silent, but an unavailable check is reported.
            "knowledge closeout unavailable, no capture performed",
        ):
            self.assertIn(needle, text,
                          f"closeout skill lost truth: {needle!r}")

    def test_closeout_agent_or_reminder_initiated_is_draft_only(self):
        """Final-critic a36b: the closeout description legitimately allows
        agent-initiated runs (after a substantial task) and the session
        hook surfaces a reminder — so Step 5.5 must NOT assume a running
        closeout was user-requested. Static rule-presence pins (honest
        text checks; no LLM-obedience claim): the draft-only rule exists
        for non-user-requested closeouts, and the false 'only runs on
        request' claim is gone."""
        text = self._norm(CLOSEOUT_SKILL)
        # The agent-initiated entry path is real and stays (the rule must
        # handle it, not pretend it away).
        self.assertIn(
            "after finishing a substantial task whose lessons would "
            "otherwise be lost", text,
            "the description's agent-initiated closeout path is the "
            "reason the draft-only rule exists")
        self.assertIn("DRAFT-ONLY", text,
                      "agent/reminder-initiated closeouts must be "
                      "draft-only: no capture, no pointer writes, no flush")
        self.assertIn("agent initiated", text)
        self.assertIn("hook reminder", text)
        # The false claim is gone (normalized check: a wrapped claim of it
        # would fail here too).
        self.assertNotIn("only runs on request", text,
                         "the skill does NOT only run on request — the "
                         "description and the hook reminder can start it")

    def test_closeout_flush_rule_matches_cli_reality(self):
        """Coordinator fix: `knowledge flush` drains the WHOLE queue (no
        own-key filter exists in the CLI), so the skill must gate a flush
        on checked status and never promise an implementation filter; a
        pending-without-enqueue pointer is re-captured, not flushed."""
        text = self._norm(CLOSEOUT_SKILL)
        for needle in (
            "drains the WHOLE knowledge queue", "own-key filter",
            "do NOT flush",
            "re-running the SAME capture (idempotent), not by a flush",
        ):
            self.assertIn(needle, text,
                          f"closeout flush rule lost truth: {needle!r}")
        self.assertNotIn("Flush only what this session enqueued", text,
                         "the CLI has no session-scoped flush — the rule "
                         "must check status, not promise a filter")

    def test_knowledge_skill_user_requested_closeout_rule(self):
        text = self._norm(KNOWLEDGE_SKILL)
        for needle in (
            "User-requested closeout",
            "not an agent-initiated after-task closeout, not a "
            "hook-reminder nudge",
            "NOT user authorization",
            "DRAFT the worthwhile note texts",
        ):
            self.assertIn(needle, text,
                          f"knowledge skill lost rule: {needle!r}")
        self.assertNotIn("USER-INVOKED closeout", text,
                         "the authorization rule keys on an actual user "
                         "request this session, not mere invocation")

    def test_closeout_skill_enabled_gated_bounded(self):
        text = self._norm(CLOSEOUT_SKILL)
        self.assertIn("Step 5.5", text)
        self.assertIn("enabled-gated", text)
        self.assertIn("silently", text,
                      "disabled companion must stay silent, not reported")
        self.assertIn("pending is not success", text,
                      "pending is not success in closeout either")
        self.assertIn("flush", text)
        # The original closeout core is untouched.
        for needle in ("## Step 0 —", "closeout-ack", "## Step 7 —"):
            self.assertIn(needle, text)

    def test_closeout_remote_no_store_claims(self):
        text = self._norm(CLOSEOUT_REMOTE_SKILL)
        self.assertIn("cannot run here", text)
        self.assertIn("Never claim a knowledge note was captured", text)
        self.assertIn("draft-only by construction", text,
                      "the remote variant never captures — say so plainly")
        self.assertIn("frozen", text,
                      "the harvest JSON key set stays frozen — knowledge "
                      "rides the reply, not the harvest")


class RealHelperSeamTest(_NodeLeg, unittest.TestCase):
    """One bounded end-to-end leg against the REAL helper once it has
    landed in this checkout (skip otherwise — the stub legs above keep
    the suite independent of the helper's landing order). Proves the
    adapter's spawn shape works against the delivered renderer: the
    helper resolves the canonical config from the inherited user flags,
    renders the enabled companion's target ids, and stays within the
    adapter's display bound."""

    def test_launcher_runpolicy_with_real_helper(self):
        policy_py = (REPO_ROOT / "skills" / "memory" / "scripts"
                     / "knowledge" / "policy.py")
        if not policy_py.is_file():
            self.skipTest("real policy.py not landed in this checkout")
        if not (self.node_on_path() and self.python_on_path()):
            self.skipTest("node/python not on PATH for the real-helper leg")
        tmp = Path(tempfile.mkdtemp(prefix="zmem-pr2-real-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        vault = tmp / "vault"
        vault.mkdir()
        (tmp / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True,
                          "targets": {"notes": {"kind": "markdown",
                                                "root": str(vault)}}}}),
            encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")
               and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}
        env.update({
            "LAUNCHER_URL": LAUNCHER_MJS.as_uri(),
            "ZMEM_ROOT": str(REPO_ROOT),
            "ZMEM_STORE": str(tmp / "store.sqlite"),
            "PYTHONIOENCODING": "utf-8",
            "PROBE_TIMEOUT_MS": "8000",
        })
        out = self.run_node(_RUNPOLICY_PROBE, env)
        self.assertEqual(out.get("reason"), "ok",
                         f"real helper leg: {out!r}")
        text = out.get("text") or ""
        self.assertTrue(text, "an enabled companion must render policy text")
        self.assertIn("notes", text,
                      "the rendered policy names the configured target id")
        self.assertLess(len(text), 8193, "within the adapter display bound")


def main(argv=None) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (HermesPolicyAppendTest, HermesRealHelperPolicyTest,
                DshLauncherPolicyTest,
                DshPreStepPolicyTest, DshDisposeDrainTest,
                RealHelperSeamTest,
                DocsAndSkillsTruthTest):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
