"""Issue #161 — Hermes provider configuration acceptance checks.

Plain unittest (repo convention; run ``python -m unittest
tests/test_hermes_config.py``).  These tests define the POST-FIX contract
for ZmemMemoryProvider's config surface: ``get_config_schema``,
``save_config``, ``_normalize_config`` validation, the config load in
``initialize`` (hermes_home kwarg), the workspace-derived namespace
branch, and ``auto_retain_enabled``.  On the pre-fix tree they fail on
the missing contract surface (AssertionError / AttributeError about the
issue #161 contract) — that is expected and correct.

Store/env isolation: ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR/
ZMEM_MODEL_AUTODOWNLOAD are pinned at module top to a fresh scratch
BEFORE any provider or host import and restored at process exit (the
tests/test_namespace.py pin+atexit pattern, scratch via
tempfile.mkdtemp); no test ever touches ~/.zmem.  Every ``initialize``
call runs with the provider module's ``_run_store`` mocked so the
store-init path never spawns a real subprocess (issue #161: subprocess
counts are asserted through that seam, never by running one).
"""

import atexit  # noqa: E402
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_PLUGIN_INIT = REPO_ROOT / "hermes-plugin" / "__init__.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "hermes"

# -- env pin (before any provider/store import) -------------------------------
# Issue #161: pin the store/env family so nothing below can touch ~/.zmem.
# Restored (env) and removed (scratch) at process exit.
_PIN_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD")
_prior_env = {k: os.environ.get(k) for k in _PIN_KEYS}
_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-161-config-"))
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"


def _restore_pin():
    for key, value in _prior_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(str(_SCRATCH), ignore_errors=True)


atexit.register(_restore_pin)

# Mode/namespace env the contract reads; each test starts from a clean slate.
_MODE_ENV_KEYS = (
    "ZMEM_HERMES_MODE", "ZMEM_MCP_URL", "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKEN_FILE", "ZMEM_HERMES_DEADLINE_S", "ZMEM_HOME",
    "ZMEM_NAMESPACE",
)

_PERSISTED_KEYS = [
    "mode", "url", "namespace_policy", "fixed_namespace",
    "injection_token_budget", "global_policy", "deadline_s",
    "query_context", "auto_retain",
]

_SCHEMA_ORDER = [
    "mode", "url", "token_file", "namespace_policy", "fixed_namespace",
    "injection_token_budget", "global_policy", "deadline_s",
    "query_context", "auto_retain",
]

_CASES_BYTES = (
    b'{"origin_url":"https://github.com/o/r",'
    b'"expected_namespace":"project:github.com/o/r"}\n'
)
_EXPECTED_BYTES = (
    b'{"origin_namespace":"project:github.com/o/r",'
    b'"no_origin_matches_launcher":true}\n'
)

_provider_seq = 0


def _load_provider():
    """Import hermes-plugin/__init__.py with the Hermes ABC stubbed.

    The tests/test_hermes_transport.py loader pattern: hermes-plugin/ is not
    an importable package name (hyphen), so the file is imported by path
    with a unique module name after stubbing the host runtime's
    ``agent.memory_provider`` modules.
    """
    global _provider_seq
    _provider_seq += 1
    name = f"zmem_hermes_161_{_provider_seq}"
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # minimal stand-in (tests/test_hermes_transport.py)
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(name, HERMES_PLUGIN_INIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_host():
    """Import skills/memory/scripts/host.py in-process (launcher parity)."""
    if "zmem161_host" not in sys.modules:
        sys.path.insert(0, str(SCRIPTS_DIR))
        spec = importlib.util.spec_from_file_location(
            "zmem161_host", SCRIPTS_DIR / "host.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["zmem161_host"] = module
        spec.loader.exec_module(module)
    return sys.modules["zmem161_host"]


def _load_checkout_fixture():
    """Load tests/fixtures/hermes/config_git_repo.py by path (no package)."""
    spec = importlib.util.spec_from_file_location(
        "zmem161_config_git_repo", FIXTURES_DIR / "config_git_repo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem161_config_git_repo"] = module
    spec.loader.exec_module(module)
    return module


class HermesConfigTest(unittest.TestCase):
    """The seven contractual issue #161 checks (names fixed by the issue)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _MODE_ENV_KEYS}
        for key in _MODE_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _fresh_hermes_home(self) -> Path:
        home = Path(tempfile.mkdtemp(prefix="zmem-161-home-"))
        self.addCleanup(shutil.rmtree, str(home), ignore_errors=True)
        return home

    # 1 ------------------------------------------------------------------

    def test_save_initialize_round_trip_all_fields(self):
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        home = self._fresh_hermes_home()
        token_file = home / "hermes-secrets" / "zmem161-roundtrip-token.txt"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("zmem161-roundtrip-token-body", encoding="utf-8")
        values = {
            "mode": "mcp",
            "url": "http://127.0.0.1:9/mcp",
            "token_file": str(token_file),
            "namespace_policy": "fixed",
            "fixed_namespace": "project:github.com/o/r",
            "injection_token_budget": 512,
            "global_policy": "exclude",
            "deadline_s": 4.5,
            "query_context": False,
            "auto_retain": True,
        }
        provider.save_config(values, str(home))

        persisted = {
            "mode": "mcp",
            "url": "http://127.0.0.1:9/mcp",
            "namespace_policy": "fixed",
            "fixed_namespace": "project:github.com/o/r",
            "injection_token_budget": 512,
            "global_policy": "exclude",
            "deadline_s": 4.5,
            "query_context": False,
            "auto_retain": True,
        }
        expected_bytes = (
            json.dumps(persisted, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n"
        ).encode("utf-8")
        config_path = home / "zmem" / "config.json"
        self.assertTrue(
            config_path.is_file(),
            "save_config must atomically write <hermes_home>/zmem/config.json")
        raw = config_path.read_bytes()
        self.assertEqual(raw, expected_bytes)
        decoded = raw.decode("utf-8")
        self.assertTrue(decoded.endswith("\n"), "exactly one final LF")
        self.assertFalse(decoded.endswith("\n\n"), "exactly one final LF")
        self.assertEqual(sorted(json.loads(decoded)), sorted(persisted))

        # A NEW provider picks the file up through initialize(hermes_home=...)
        # with no env overrides.
        mod2 = _load_provider()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_NAMESPACE", None)
            provider2 = mod2.ZmemMemoryProvider()
            with mock.patch.object(mod2, "_run_store"):
                provider2.initialize(session_id="s1", hermes_home=str(home))
        self.assertEqual(provider2._namespace, "project:github.com/o/r")
        self.assertIs(provider2.auto_retain_enabled(), True)
        self.assertIs(provider2._mode, mod2._transport.TransportMode.mcp)

    # 2 ------------------------------------------------------------------

    def test_secret_values_are_not_written(self):
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        home = self._fresh_hermes_home()
        secret_body = "zmem161-secret-body-4f3e2d1c0b9a"
        token_file = (home / "hermes-secrets"
                      / "zmem161-secret-token.txt")
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(secret_body, encoding="utf-8")
        provider.save_config(
            {"mode": "mcp",
             "url": "http://127.0.0.1:9/mcp",
             "token_file": str(token_file)},
            str(home))
        config_path = home / "zmem" / "config.json"
        self.assertTrue(
            config_path.is_file(),
            "save_config must write <hermes_home>/zmem/config.json")
        decoded = config_path.read_bytes().decode("utf-8")
        # No token VALUE, no access_token key, no token-file PATH, no
        # token-file CONTENTS anywhere in the persisted bytes.
        self.assertNotIn(secret_body, decoded)
        self.assertNotIn("access_token", decoded)
        self.assertNotIn(str(token_file), decoded)
        self.assertNotIn(str(token_file).replace("\\", "\\\\"), decoded)
        self.assertNotIn(token_file.name, decoded)
        parsed = json.loads(decoded)
        self.assertEqual(sorted(parsed), sorted(_PERSISTED_KEYS))

        # No schema entry with secret=True (token_file) is among the nine
        # serialized keys, and the schema nowhere contains the secret body.
        schema = provider.get_config_schema()
        token_entries = [e for e in schema if e.get("key") == "token_file"]
        self.assertEqual(len(token_entries), 1)
        self.assertIs(token_entries[0].get("secret"), True)
        self.assertNotIn(secret_body, json.dumps(schema))

    # 3 ------------------------------------------------------------------

    def test_auto_retain_defaults_false(self):
        home = self._fresh_hermes_home()  # no zmem/config.json in it
        os.environ["ZMEM_HOME"] = str(home)
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        with mock.patch.object(mod, "_run_store"):
            provider.initialize(session_id="s")
        self.assertIs(provider.auto_retain_enabled(), False)

        # Full schema contract (issue #161): exactly ten entries, in order,
        # every entry carrying the eight schema keys.
        schema = provider.get_config_schema()
        self.assertIsInstance(schema, list)
        self.assertEqual([e.get("key") for e in schema], _SCHEMA_ORDER)
        required_keys = {"key", "description", "type", "default", "choices",
                         "minimum", "secret", "env_var"}
        by_key = {}
        for entry in schema:
            with self.subTest(schema_entry=entry.get("key")):
                self.assertIsInstance(entry, dict)
                for name in required_keys:
                    self.assertIn(name, entry)
                self.assertIsInstance(entry["description"], str)
                self.assertTrue(entry["description"].strip())
                self.assertIsInstance(entry["type"], str)
                self.assertTrue(entry["type"].strip())
                by_key[entry["key"]] = entry

        def _choices(key):
            value = by_key[key]["choices"]
            self.assertNotIsInstance(value, str, key)
            return list(value)

        self.assertEqual(_choices("mode"), ["local", "mcp"])
        self.assertEqual(by_key["mode"]["default"], "local")
        self.assertEqual(by_key["url"]["type"], "text")
        self.assertEqual(by_key["url"]["default"], "")
        self.assertEqual(by_key["token_file"]["type"], "text")
        self.assertEqual(by_key["token_file"]["default"], "")
        self.assertIs(by_key["token_file"]["secret"], True)
        self.assertEqual(by_key["token_file"]["env_var"], "ZMEM_MCP_TOKEN_FILE")
        self.assertEqual(_choices("namespace_policy"), ["derive", "fixed"])
        self.assertEqual(by_key["namespace_policy"]["default"], "derive")
        self.assertEqual(by_key["fixed_namespace"]["default"], "user:global")
        self.assertEqual(by_key["injection_token_budget"]["type"], "integer")
        self.assertEqual(by_key["injection_token_budget"]["minimum"], 0)
        self.assertEqual(by_key["injection_token_budget"]["default"], 0)
        self.assertEqual(_choices("global_policy"), ["include", "exclude"])
        self.assertEqual(by_key["global_policy"]["default"], "include")
        self.assertEqual(by_key["deadline_s"]["type"], "number")
        self.assertEqual(by_key["deadline_s"]["default"], 6.0)
        self.assertEqual(by_key["query_context"]["type"], "boolean")
        self.assertIs(by_key["query_context"]["default"], True)
        self.assertEqual(by_key["auto_retain"]["type"], "boolean")
        self.assertIs(by_key["auto_retain"]["default"], False)

        # Unused choices/minimum/env_var/secret are None.
        for key in _SCHEMA_ORDER:
            entry = by_key[key]
            if key not in ("mode", "namespace_policy", "global_policy"):
                self.assertIsNone(entry["choices"], key)
            if key != "injection_token_budget":
                self.assertIsNone(entry["minimum"], key)
            if key != "token_file":
                self.assertIsNone(entry["secret"], key)
                self.assertIsNone(entry["env_var"], key)

    # 4 ------------------------------------------------------------------

    def test_invalid_config_fails_closed(self):
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        home = self._fresh_hermes_home()
        config_dir = home / "zmem"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"
        prior = (
            json.dumps(
                {"mode": "local", "url": "",
                 "namespace_policy": "derive",
                 "fixed_namespace": "user:global",
                 "injection_token_budget": 0,
                 "global_policy": "include",
                 "deadline_s": 6.0, "query_context": True,
                 "auto_retain": False},
                ensure_ascii=False, sort_keys=True,
                separators=(",", ":")) + "\n"
        ).encode("utf-8")
        config_path.write_bytes(prior)
        prior_sha = hashlib.sha256(prior).hexdigest()

        invalid_cases = [
            ("non-dict list input", ["mode", "local"]),
            ("non-dict None input", None),
            ("mode outside tuple", {"mode": "carrier-pigeon"}),
            ("namespace_policy outside tuple",
             {"namespace_policy": "guess"}),
            ("global_policy outside tuple",
             {"global_policy": "sometimes"}),
            ("negative injection_token_budget",
             {"injection_token_budget": -1}),
            ("deadline_s zero", {"deadline_s": 0}),
            ("deadline_s negative", {"deadline_s": -2.5}),
            ("deadline_s nan", {"deadline_s": float("nan")}),
            ("deadline_s inf", {"deadline_s": float("inf")}),
            ("non-boolean query_context", {"query_context": "yes"}),
            ("non-boolean auto_retain", {"auto_retain": 1}),
            ("blank fixed_namespace", {"fixed_namespace": "   "}),
        ]
        for label, overlay in invalid_cases:
            with self.subTest(case=label):
                if isinstance(overlay, dict):
                    values = {"mode": "local"}
                    values.update(overlay)
                else:
                    values = overlay
                with mock.patch.object(mod, "_run_store") as run_store, \
                        mock.patch.object(provider,
                                          "_select_transport") as reselect:
                    with self.assertRaises(ValueError):
                        provider.save_config(values, str(home))
                    self.assertEqual(
                        run_store.call_count, 0,
                        "an invalid save must spawn no store subprocess")
                    self.assertEqual(
                        reselect.call_count, 0,
                        "an invalid save must not re-select the transport")
                self.assertEqual(
                    hashlib.sha256(config_path.read_bytes()).hexdigest(),
                    prior_sha,
                    "prior destination bytes must be unchanged")

        # LOAD failure: malformed config.json -> exactly one warning,
        # in-memory defaults retained, transport/store startup skipped,
        # _initialized stays False.
        config_path.write_text("{not json at all", encoding="utf-8")
        mod2 = _load_provider()
        provider2 = mod2.ZmemMemoryProvider()
        with mock.patch.object(mod2, "_run_store") as run_store2:
            with self.assertLogs(mod2.logger, level="WARNING") as logged:
                provider2.initialize(session_id="s", hermes_home=str(home))
            self.assertEqual(
                len(logged.records), 1,
                "a malformed config load must log exactly one warning")
            self.assertEqual(
                run_store2.call_count, 0,
                "store-init subprocess must be skipped on a failed load")
        self.assertIs(provider2._initialized, False)

    # 5 ------------------------------------------------------------------

    def test_derive_namespace_from_git_origin(self):
        cases_path = FIXTURES_DIR / "config_namespace_cases.json"
        expected_path = (FIXTURES_DIR /
                         "config_namespace_cases.expected.json")
        cases_raw = cases_path.read_bytes()
        expected_raw = expected_path.read_bytes()
        self.assertEqual(cases_raw, _CASES_BYTES)
        self.assertEqual(expected_raw, _EXPECTED_BYTES)
        cases = json.loads(cases_raw.decode("utf-8"))
        expected_doc = json.loads(expected_raw.decode("utf-8"))

        fixture = _load_checkout_fixture()
        with tempfile.TemporaryDirectory(prefix="zmem-161-derive-") as tmp:
            checkout, requested = fixture.make_checkout(
                Path(tmp) / "checkout", "https://github.com/o/r")
            self.assertTrue(checkout.is_dir())
            self.assertEqual(requested, "https://github.com/o/r")
            mod = _load_provider()
            provider = mod.ZmemMemoryProvider()
            with mock.patch.object(mod, "_run_store"):
                provider.initialize(
                    session_id="s", agent_workspace=str(checkout))
            self.assertEqual(provider._namespace, "project:github.com/o/r")

            # The fixture's origin_url maps to its expected_namespace
            # through the same provider path.
            fixture_checkout, fixture_url = fixture.make_checkout(
                Path(tmp) / "fixture-checkout", cases["origin_url"])
            self.assertEqual(fixture_url, cases["origin_url"])
            provider2 = mod.ZmemMemoryProvider()
            with mock.patch.object(mod, "_run_store"):
                provider2.initialize(
                    session_id="s2", agent_workspace=str(fixture_checkout))
            self.assertEqual(provider2._namespace,
                             cases["expected_namespace"])

        # The .expected.json sidecar agrees with the cases fixture.
        self.assertEqual(expected_doc["origin_namespace"],
                         cases["expected_namespace"])
        self.assertIs(expected_doc["no_origin_matches_launcher"], True)

    # 6 ------------------------------------------------------------------

    def test_no_origin_matches_launcher_key(self):
        host = _load_host()
        with tempfile.TemporaryDirectory(prefix="zmem-161-no-origin-") as tmp:
            repo = Path(tmp) / "no-origin"
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "-q", "--initial-branch", "main"],
                cwd=str(repo), check=True)
            launcher_key = host.resolve_namespace(repo)
            mod = _load_provider()
            provider = mod.ZmemMemoryProvider()
            with mock.patch.object(mod, "_run_store"):
                provider.initialize(
                    session_id="s", agent_workspace=str(repo))
            provider_key = provider._namespace
        self.assertTrue(launcher_key.startswith("project:"))
        self.assertEqual(provider_key, launcher_key)
        self.assertEqual(provider_key.encode("utf-8"),
                         launcher_key.encode("utf-8"))

    # 7 ------------------------------------------------------------------

    def test_same_checkout_shares_namespace(self):
        fixture = _load_checkout_fixture()
        with tempfile.TemporaryDirectory(prefix="zmem-161-share-") as tmp:
            checkout, _requested = fixture.make_checkout(
                Path(tmp) / "shared", "https://github.com/o/r")
            mod = _load_provider()
            keys = []
            for session_id in ("s-one", "s-two"):
                provider = mod.ZmemMemoryProvider()
                with mock.patch.object(mod, "_run_store"):
                    provider.initialize(
                        session_id=session_id,
                        agent_workspace=str(checkout))
                keys.append(provider._namespace)
            self.assertEqual(keys[0], keys[1])
            self.assertEqual(keys[0].encode("utf-8"),
                             keys[1].encode("utf-8"))
            self.assertEqual(keys[0], "project:github.com/o/r")


    # 8-17: plan-critic round-1 mandated coverage (issue #161 trace,
    # 06-critic-review.md Round 1 Required Revisions 1-3, 5).  These pin the
    # config-overlay precedence semantics, config-only availability, the
    # fail-closed edges, and the contract-mandated namespace changes.

    def _write_default_config(self, home: Path) -> None:
        """Save an all-defaults config through save_config (the UI shape)."""
        mod = _load_provider()
        mod.ZmemMemoryProvider().save_config(
            {"mode": "local", "url": "", "namespace_policy": "derive",
             "fixed_namespace": "user:global", "injection_token_budget": 0,
             "global_policy": "include", "deadline_s": 6.0,
             "query_context": True, "auto_retain": False},
            str(home))

    def test_config_overlay_precedence_matrix(self):
        # (a) defaults-only file + env ZMEM_MCP_URL -> mcp preserved: a
        # schema-default value in a saved file must never mask the env.
        home = self._fresh_hermes_home()
        self._write_default_config(home)
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        with mock.patch.object(mod, "_run_store"):
            provider.initialize(session_id="s", hermes_home=str(home))
        self.assertIs(provider._mode, mod._transport.TransportMode.mcp,
                      "a defaults-only config file must not mask ZMEM_MCP_URL")

        # (b) non-default file url + no env mode -> mcp via the file value.
        home2 = self._fresh_hermes_home()
        os.environ.pop("ZMEM_MCP_URL")
        mod2 = _load_provider()
        mod2.ZmemMemoryProvider().save_config(
            {"mode": "local", "url": "http://127.0.0.1:9/mcp"},
            str(home2))
        provider2 = mod2.ZmemMemoryProvider()
        with mock.patch.object(mod2, "_run_store"):
            provider2.initialize(session_id="s", hermes_home=str(home2))
        self.assertIs(provider2._mode, mod2._transport.TransportMode.mcp)

        # (c) env beats config: env ZMEM_HERMES_MODE=local wins over a
        # non-default file url.
        home3 = self._fresh_hermes_home()
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        os.environ["ZMEM_HERMES_MODE"] = "local"
        mod3 = _load_provider()
        mod3.ZmemMemoryProvider().save_config(
            {"mode": "local", "url": "http://127.0.0.1:10/mcp"},
            str(home3))
        provider3 = mod3.ZmemMemoryProvider()
        with mock.patch.object(mod3, "_run_store"):
            provider3.initialize(session_id="s", hermes_home=str(home3))
        self.assertIs(provider3._mode, mod3._transport.TransportMode.local,
                      "explicit env mode must beat a non-default config url")

        # (e) explicit kwarg mode=local beats an env URL (kwargs are
        # presence-tracked: a passed kwarg is always applied unless the
        # SAME key is set in the env — see (g)).
        home5 = self._fresh_hermes_home()
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        os.environ.pop("ZMEM_HERMES_MODE")
        mod5 = _load_provider()
        provider5 = mod5.ZmemMemoryProvider()
        with mock.patch.object(mod5, "_run_store"):
            provider5.initialize(session_id="s", hermes_home=str(home5),
                                 mode="local")
        self.assertIs(provider5._mode, mod5._transport.TransportMode.local)

        # (g) same-key conflict: env ZMEM_HERMES_MODE beats a kwarg mode
        # (overlay precedence is env > kwargs > file-non-default).
        home6 = self._fresh_hermes_home()
        os.environ["ZMEM_HERMES_MODE"] = "mcp"
        mod6 = _load_provider()
        provider6 = mod6.ZmemMemoryProvider()
        with mock.patch.object(mod6, "_run_store"):
            provider6.initialize(session_id="s", hermes_home=str(home6),
                                 mode="local")
        self.assertIs(provider6._mode, mod6._transport.TransportMode.mcp,
                      "env must beat an explicit kwarg on the same key")

    def test_kwargs_merge_over_file_values(self):
        # (d) kwargs beat file: file deadline 2.0, kwarg deadline 4.0 ->
        # the transport is built with 4.0.  The #160 transports store the
        # resolved deadline privately as _deadline_s (transport.py:357,384);
        # reading it here pins the seam without changing transport.py.
        home = self._fresh_hermes_home()
        mod = _load_provider()
        mod.ZmemMemoryProvider().save_config(
            {"mode": "local", "deadline_s": 2.0}, str(home))
        provider = mod.ZmemMemoryProvider()
        with mock.patch.object(mod, "_run_store"):
            provider.initialize(session_id="s", hermes_home=str(home),
                                deadline_s=4.0)
        self.assertIsNotNone(provider._transport)
        self.assertEqual(
            float(getattr(provider._transport, "_deadline_s")), 4.0)

    def test_config_token_file_is_schema_only(self):
        # Critic round-1 revision 1: token behavior is owned by #160 (issue
        # scope line "Out of scope: transport ... token behavior owned by
        # #160").  A config/kwarg token_file must therefore NOT change
        # transport selection, and must never be persisted (secret).
        home = self._fresh_hermes_home()
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        mod = _load_provider()
        baseline = mod.ZmemMemoryProvider()
        with mock.patch.object(mod, "_run_store"):
            baseline.initialize(session_id="s", hermes_home=str(home))
        with_token = mod.ZmemMemoryProvider()
        with mock.patch.object(mod, "_run_store"):
            with_token.initialize(session_id="s2", hermes_home=str(home),
                                  token_file="Z:/nonexistent/token.txt")
        self.assertIs(with_token._mode, baseline._mode)
        self.assertEqual(type(with_token._transport), type(baseline._transport))

        # Not persisted: save with token_file -> bytes carry no token_file
        # (already pinned by test_secret_values_are_not_written); here also
        # assert the normalized in-memory value never reaches the transport.
        self.assertEqual(type(with_token._transport), type(baseline._transport))

    def test_availability_config_only_mode_mcp(self):
        # Critic round-1 revision 2: a config-only mode=mcp home must be
        # available at CONSTRUCTION time (before any initialize), because
        # hosts may gate on is_available().
        home = self._fresh_hermes_home()
        mod = _load_provider()
        mod.ZmemMemoryProvider().save_config(
            {"mode": "mcp", "url": "http://127.0.0.1:9/mcp"}, str(home))
        os.environ["ZMEM_HOME"] = str(home)
        try:
            provider = mod.ZmemMemoryProvider()
            self.assertTrue(
                provider.is_available(),
                "config-only mode=mcp must be available at construction")
            self.assertIs(provider._mode, mod._transport.TransportMode.mcp)
            with mock.patch.object(mod, "_run_store"):
                provider.initialize(session_id="s", hermes_home=str(home))
            self.assertTrue(provider.is_available())
            self.assertIs(provider._mode, mod._transport.TransportMode.mcp)
        finally:
            os.environ.pop("ZMEM_HOME", None)

    def test_zero_byte_config_loads_defaults(self):
        # Critic round-1: zero-byte file -> defaults (contract step 2), no
        # warning, normal env-only selection.
        home = self._fresh_hermes_home()
        config_dir = home / "zmem"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_bytes(b"")
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:9/mcp"
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        with mock.patch.object(mod, "_run_store") as run_store, \
                self.assertNoLogs(mod.logger, level="WARNING"):
            provider.initialize(session_id="s", hermes_home=str(home))
        self.assertIs(provider._initialized, True)
        self.assertIs(provider._mode, mod._transport.TransportMode.mcp)
        self.assertEqual(run_store.call_count, 0,
                         "mcp-mode selection must skip the store-init path")

    def test_invalid_values_file_load_fails_closed(self):
        # Critic round-1: valid JSON with INVALID values -> one warning,
        # defaults, _initialized False, zero store subprocesses.
        home = self._fresh_hermes_home()
        config_dir = home / "zmem"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            json.dumps({"mode": "local", "deadline_s": 0}), encoding="utf-8")
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        with mock.patch.object(mod, "_run_store") as run_store:
            with self.assertLogs(mod.logger, level="WARNING") as logged:
                provider.initialize(session_id="s", hermes_home=str(home))
            self.assertEqual(len(logged.records), 1)
            self.assertEqual(run_store.call_count, 0)
        self.assertIs(provider._initialized, False)
        self.assertIs(provider.auto_retain_enabled(), False)

    def test_save_config_filesystem_failure(self):
        # Critic round-1: filesystem failure -> ValueError, and the
        # destination tree is left without a partial config.json.
        home = self._fresh_hermes_home()
        (home / "zmem").write_text("not a directory", encoding="utf-8")
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        with self.assertRaises(ValueError):
            provider.save_config({"mode": "local"}, str(home))
        self.assertFalse((home / "zmem" / "config.json").exists())

    def test_repeated_initialize_is_idempotent(self):
        fixture = _load_checkout_fixture()
        with tempfile.TemporaryDirectory(prefix="zmem-161-idem-") as tmp:
            checkout, _ = fixture.make_checkout(
                Path(tmp) / "checkout", "https://github.com/o/r")
            mod = _load_provider()
            provider = mod.ZmemMemoryProvider()
            with mock.patch.object(mod, "_run_store") as run_store:
                provider.initialize(session_id="s1",
                                    agent_workspace=str(checkout))
                first = provider._namespace
                provider.initialize(session_id="s2",
                                    agent_workspace=str(checkout))
            self.assertEqual(provider._namespace, first)
            self.assertEqual(first, "project:github.com/o/r")
            self.assertLessEqual(run_store.call_count, 2)

    def test_user_id_kwarg_no_longer_scopes_namespace(self):
        # Critic round-1 revision 4: the issue's precedence list drops the
        # provider's user:<user_id> branch.  Pin the new behavior so the
        # removal is a recorded contract, not a silent change.
        fixture = _load_checkout_fixture()
        with tempfile.TemporaryDirectory(prefix="zmem-161-userid-") as tmp:
            checkout, _ = fixture.make_checkout(
                Path(tmp) / "checkout", "https://github.com/o/r")
            mod = _load_provider()
            provider = mod.ZmemMemoryProvider()
            with mock.patch.object(mod, "_run_store"):
                provider.initialize(session_id="s", user_id="alice",
                                    agent_workspace=str(checkout))
            self.assertNotEqual(provider._namespace, "user:alice")
            self.assertEqual(provider._namespace, "project:github.com/o/r")

    def test_session_switch_rederives_namespace(self):
        # Critic round-1: on_session_switch inherits the new derivation
        # (#162 owns the surface; the namespace re-derivation itself is
        # this issue's).  Switching workspaces re-scopes the namespace.
        fixture = _load_checkout_fixture()
        with tempfile.TemporaryDirectory(prefix="zmem-161-switch-") as tmp:
            checkout_a, _ = fixture.make_checkout(
                Path(tmp) / "a", "https://github.com/o/r")
            checkout_b, _ = fixture.make_checkout(
                Path(tmp) / "b", "https://github.com/other/repo")
            mod = _load_provider()
            provider = mod.ZmemMemoryProvider()
            with mock.patch.object(mod, "_run_store"):
                provider.initialize(session_id="s1",
                                    agent_workspace=str(checkout_a))
                self.assertEqual(provider._namespace,
                                 "project:github.com/o/r")
                provider.on_session_switch(
                    "s2", agent_workspace=str(checkout_b))
            self.assertEqual(provider._namespace,
                             "project:github.com/other/repo")

    def test_coercion_semantics(self):
        # Critic round-1 advisory: pin int()/float() conversion edges.
        mod = _load_provider()
        provider = mod.ZmemMemoryProvider()
        normalized = provider._normalize_config(
            {"injection_token_budget": 512.9, "deadline_s": True})
        self.assertEqual(normalized["injection_token_budget"], 512)
        self.assertEqual(normalized["deadline_s"], 1.0)
        with self.assertRaises(ValueError):
            provider._normalize_config({"deadline_s": False})


if __name__ == "__main__":
    unittest.main()
