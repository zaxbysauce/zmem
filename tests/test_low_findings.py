"""Regression tests for issue #37 low-severity codebase-review findings.

Covers the behavior-changing fixes:
  L1  — export-pack docstring no longer claims structural text is "exempt"
  L7  — Hermes `_tool_add` rejects an invalid signal with a clean message
  L8  — Hermes `_tool_add` rejects oversize content with a clean message
  L10 — `bind_guard._is_wildcard` flags IPv4-mapped IPv6 wildcard forms
  L11 — missing `agent.memory_provider` raises a clear, actionable ImportError
  L21 — `stats` surfaces last_backup / last_consolidation operational health
  L23 — `doctor` reports backup/consolidation cadence health (never/stale/recent)
  L25 — `_assert_local_fs` is a single shared import (3 hooks) and the fail-open
        branch still protects against unexpected exceptions

Run: python tests/test_low_findings.py
No pytest / third-party harness required — matches the repo convention.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
HERMES_DIR = REPO_ROOT / "hermes-plugin"
HOOKS_DIR = HERMES_DIR / "hooks"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable


def _env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    return env


def _run(env: dict, *args: str, timeout: int = 60):
    return subprocess.run(
        [PYTHON, str(STORE_PY), *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


import subprocess  # noqa: E402


def _posix_bash_available() -> bool:
    """True if `bash -c 'echo ok'` works (a POSIX bash is on PATH). Some dev
    boxes route `bash` to WSL (which fails under Git Bash); behavioral shell
    tests skip when no working bash is reachable."""
    try:
        r = subprocess.run(["bash", "-c", "echo ok"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and r.stdout.strip() == "ok"
    except Exception:
        return False


POSIX_BASH = _posix_bash_available()


# ---------------------------------------------------------------------------
# L1 — export-pack docstring no longer claims structural text is "exempt"
# ---------------------------------------------------------------------------
class L1ExportPackDocstring(unittest.TestCase):
    def test_render_pack_docstring_does_not_claim_exempt(self):
        src = STORE_PY.read_text(encoding="utf-8")
        self.assertIn("def _render_pack(", src)
        # The stale claim ("exempt from the cap") must be gone — the budget
        # code counts all accumulated lines including structural framing.
        self.assertNotIn("exempt from the cap", src)


# ---------------------------------------------------------------------------
# L10 — bind_guard._is_wildcard flags IPv4-mapped IPv6 wildcard forms
# ---------------------------------------------------------------------------
class L10Ipv4MappedWildcard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(HERMES_DIR / "server"))
        import bind_guard  # type: ignore
        cls.bg = bind_guard

    def _wildcard(self, host):
        return self.bg._is_wildcard(host)

    def test_mapped_unspecified_ipv6_is_wildcard(self):
        # ::ffff:0.0.0.0 is the IPv4-mapped form of 0.0.0.0. is_unspecified is
        # False for the mapped address, so without the L10 fix this returns
        # False (the gap). All three equivalent literals must be flagged.
        for h in ("::ffff:0.0.0.0", "::ffff:0:0",
                  "0:0:0:0:0:ffff:0.0.0.0", "[::ffff:0.0.0.0]"):
            with self.subTest(host=h):
                self.assertTrue(self._wildcard(h),
                                f"{h!r} should be treated as a wildcard bind")

    def test_mapped_specified_ipv6_is_not_wildcard(self):
        # ::ffff:127.0.0.1 maps to a real address — must NOT be flagged.
        for h in ("::ffff:127.0.0.1", "::ffff:8.8.8.8"):
            with self.subTest(host=h):
                self.assertFalse(self._wildcard(h),
                                 f"{h!r} must not be treated as a wildcard bind")

    def test_classic_wildcard_forms_still_detected(self):
        # Regression guard: the pre-existing detections must still work.
        for h in ("0.0.0.0", "::", "[::]", "0", "0.0", "",
                  "0000:0000:0000:0000:0000:0000:0000:0000"):
            with self.subTest(host=h):
                self.assertTrue(self._wildcard(h), f"{h!r} should be wildcard")

    def test_non_wildcard_hosts_pass(self):
        for h in ("127.0.0.1", "1.2.3.4", "::1", "192.168.1.5"):
            with self.subTest(host=h):
                self.assertFalse(self._wildcard(h), f"{h!r} must not be wildcard")


# ---------------------------------------------------------------------------
# L11 — missing agent.memory_provider raises a clear, actionable ImportError
# ---------------------------------------------------------------------------
class L11MemoryProviderImport(unittest.TestCase):
    def _load_hermes_init(self):
        import importlib
        for mod in list(sys.modules):
            if mod == "zmem_hermes_init_l11":
                del sys.modules[mod]
        spec = importlib.util.spec_from_file_location(
            "zmem_hermes_init_l11", str(HERMES_DIR / "__init__.py"))
        return importlib.util.module_from_spec(spec), spec

    def test_missing_memory_provider_gives_actionable_message(self):
        # Simulate the host runtime NOT providing agent.memory_provider. The
        # import guard must raise an ImportError whose message names the module
        # and explains it is host-provided (so a user doesn't go hunting for a
        # pip package).
        saved = {}
        for mod in list(sys.modules):
            if mod == "agent" or mod.startswith("agent."):
                saved[mod] = sys.modules.pop(mod)
        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *a, **k):
            if name == "agent" or name.startswith("agent."):
                # The real failure mode: ModuleNotFoundError for the top-level
                # 'agent' package (what Python raises when the host doesn't
                # provide it). Must carry .name so the guard can recognize it.
                raise ModuleNotFoundError(
                    f"No module named '{name.split('.')[0]}'",
                    name=name.split('.')[0])
            return real_import(name, *a, **k)
        try:
            builtins.__import__ = blocking_import
            mod, spec = self._load_hermes_init()
            with self.assertRaises(ImportError) as cm:
                spec.loader.exec_module(mod)
            msg = str(cm.exception)
            self.assertIn("agent.memory_provider", msg)
            self.assertIn("Hermes host", msg)
        finally:
            builtins.__import__ = real_import
            sys.modules.update(saved)

    def test_transitive_import_error_is_not_misreported(self):
        # PRR-003: if agent.memory_provider IS importable as a top-level package
        # but has a broken transitive dependency inside it (raises
        # ModuleNotFoundError for a DIFFERENT module), the guard must NOT swallow
        # it into the "host module missing" message — it must propagate the
        # original error with its real .name so the operator sees the true cause.
        saved = {}
        for mod in list(sys.modules):
            if mod == "agent" or mod.startswith("agent."):
                saved[mod] = sys.modules.pop(mod)
        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *a, **k):
            # 'agent' imports fine (so the guard's name-check sees agent as
            # present), but 'agent.memory_provider' itself raises a
            # ModuleNotFoundError for a DIFFERENT transitive dep.
            if name == "agent":
                mod = real_import(name, *a, **k)
                return mod
            if name == "agent.memory_provider":
                raise ModuleNotFoundError(
                    "No module named 'some_broken_subdep'",
                    name="some_broken_subdep")
            return real_import(name, *a, **k)
        try:
            builtins.__import__ = blocking_import
            mod, spec = self._load_hermes_init()
            with self.assertRaises(ModuleNotFoundError) as cm:
                spec.loader.exec_module(mod)
            # The transitive error must propagate with its real .name, NOT be
            # wrapped in the "agent.memory_provider ... Hermes host" message.
            self.assertEqual(cm.exception.name, "some_broken_subdep")
            self.assertNotIn("Hermes host", str(cm.exception))
        finally:
            builtins.__import__ = real_import
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# L21 — stats surfaces last_backup / last_consolidation
# ---------------------------------------------------------------------------
class L21StatsOperationalHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-l21-")
        self.env = _env(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stats_shows_never_on_fresh_store(self):
        # A store that has never run backup/consolidation must surface "(never)".
        r = _run(self.env, "stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("operational health:", r.stdout)
        self.assertIn("last_backup: (never)", r.stdout)
        self.assertIn("last_consolidation: (never)", r.stdout)

    def test_stats_shows_timestamp_after_backup(self):
        # Running backup must record last_backup; stats must then show a value.
        r1 = _run(self.env, "backup", "--retention", "1")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = _run(self.env, "stats")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("last_backup:", r2.stdout)
        self.assertNotIn("last_backup: (never)", r2.stdout)
        # E1 (#39) added relative recency ("just now"/"Nd ago") before the raw
        # ISO timestamp, which is now in parentheses. The ISO value must still
        # be present so the exact time is recoverable. The recency prefix is
        # matched explicitly (not a .+ wildcard) so a regression that drops it
        # fails (PRR-010).
        self.assertRegex(
            r2.stdout,
            r"last_backup: (just now|\d+[mhd] ago) \(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\)",
        )


# ---------------------------------------------------------------------------
# L23 — doctor reports backup/consolidation cadence health
# ---------------------------------------------------------------------------
class L23DoctorOperationalHealth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS_DIR))
        import doctor  # type: ignore
        cls.doctor = doctor

    def _fresh_store(self):
        tmp = tempfile.mkdtemp(prefix="zmem-l23-")
        store = Path(tmp) / "store.sqlite"
        # Initialize a minimal store with a meta table.
        conn = sqlite3.connect(str(store))
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        return tmp, store

    def _set_meta(self, store: Path, key: str, days_ago: float):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - days_ago * 86400))
        conn = sqlite3.connect(str(store))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, ts))
        conn.commit()
        conn.close()

    def test_warn_on_never_run(self):
        tmp, store = self._fresh_store()
        try:
            checks = self.doctor._check_operational_health(store)
            statuses = {c["id"]: c["status"] for c in checks}
            self.assertEqual(statuses.get("operational-health-backup"), "warn")
            self.assertEqual(statuses.get("operational-health-consolidation"), "warn")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pass_on_recent(self):
        tmp, store = self._fresh_store()
        self._set_meta(store, "last_backup", 0.1)       # ~2.4h ago, within 2-day cadence
        self._set_meta(store, "last_consolidation", 1.0)  # 1d ago, within 14-day cadence
        try:
            checks = self.doctor._check_operational_health(store)
            statuses = {c["id"]: c["status"] for c in checks}
            self.assertEqual(statuses.get("operational-health-backup"), "pass")
            self.assertEqual(statuses.get("operational-health-consolidation"), "pass")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_warn_on_stale(self):
        tmp, store = self._fresh_store()
        self._set_meta(store, "last_backup", 5.0)          # 5d > 2d threshold
        self._set_meta(store, "last_consolidation", 30.0)  # 30d > 14d threshold
        try:
            checks = self.doctor._check_operational_health(store)
            statuses = {c["id"]: c["status"] for c in checks}
            self.assertEqual(statuses.get("operational-health-backup"), "warn")
            self.assertEqual(statuses.get("operational-health-consolidation"), "warn")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_skip_on_absent_store(self):
        checks = self.doctor._check_operational_health(Path("/no/such/store.sqlite"))
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "skip")

    def test_warn_on_unreadable_timestamp(self):
        # PRR-013: a meta row present but with an unparseable timestamp must
        # warn (not crash), surfacing the bad value.
        tmp, store = self._fresh_store()
        conn = sqlite3.connect(str(store))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_backup', ?)",
                     ("not-a-timestamp",))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_consolidation', ?)",
                     ("also-bad",))
        conn.commit()
        conn.close()
        try:
            checks = self.doctor._check_operational_health(store)
            statuses = {c["id"]: c["status"] for c in checks}
            self.assertEqual(statuses.get("operational-health-backup"), "warn")
            self.assertEqual(statuses.get("operational-health-consolidation"), "warn")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_clamps_negative_cadence_to_default(self):
        # PRR-002: a negative ZMEM_BACKUP_INTERVAL_DAYS must not produce a
        # negative/always-true warn threshold. Doctor should clamp it to the
        # default (1d -> 2d warn), matching store.py's behavior.
        tmp, store = self._fresh_store()
        self._set_meta(store, "last_backup", 0.1)  # recent
        try:
            with mock.patch.dict(os.environ, {"ZMEM_BACKUP_INTERVAL_DAYS": "-5"}):
                checks = self.doctor._check_operational_health(store)
                statuses = {c["id"]: c["status"] for c in checks}
                # With the clamp, -5 -> default 1.0 -> warn_days 2.0; 0.1d is recent -> pass.
                self.assertEqual(statuses.get("operational-health-backup"), "pass",
                                 f"negative cadence should clamp to default, got {statuses}")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_skip_on_store_without_meta_table(self):
        # PRR-013: a store that exists but has NO meta table (or is otherwise
        # unreadable via the ro connection) must skip gracefully, not crash.
        tmp = tempfile.mkdtemp(prefix="zmem-l23nometa-")
        store = Path(tmp) / "store.sqlite"
        # Create a sqlite file with a different schema (no meta table).
        conn = sqlite3.connect(str(store))
        conn.execute("CREATE TABLE other(x INTEGER)")
        conn.commit()
        conn.close()
        try:
            checks = self.doctor._check_operational_health(store)
            # Either skip (open succeeded but meta read failed -> the unreadable
            # warn path) or skip (open failed). Both are non-crash outcomes; the
            # key assertion is no exception and both entries present.
            ids = {c["id"] for c in checks}
            self.assertTrue(
                ids >= {"operational-health-backup", "operational-health-consolidation"}
                or any(c["status"] == "skip" for c in checks),
                f"expected health checks or a skip, got {ids}")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# L25 — shared _assert_local_fs (3 hooks import it; fail-open still protects)
# ---------------------------------------------------------------------------
class L25SharedAssertLocalFs(unittest.TestCase):
    def test_all_three_hooks_import_shared_helper_not_define_locally(self):
        import ast
        for hook in ("zmem-hermes-convention.py",
                     "zmem-hermes-reflect.py",
                     "zmem-hermes-verify.py"):
            with self.subTest(hook=hook):
                src = (HOOKS_DIR / hook).read_text(encoding="utf-8")
                tree = ast.parse(src)
                defs = {n.name for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)}
                imps = [n for n in ast.walk(tree)
                        if isinstance(n, ast.ImportFrom)
                        and n.module == "_zmem_hook_common"]
                self.assertNotIn("_assert_local_fs", defs,
                                 f"{hook} still defines _assert_local_fs locally")
                self.assertTrue(imps, f"{hook} does not import from _zmem_hook_common")

    def test_shared_helper_rejects_unc_and_accepts_local(self):
        sys.path.insert(0, str(HOOKS_DIR))
        from _zmem_hook_common import assert_local_fs  # type: ignore
        # Forward-slash UNC form must be rejected.
        self.assertFalse(assert_local_fs(Path("//server/share/store.sqlite")))
        # A real local temp dir must be accepted.
        self.assertTrue(assert_local_fs(Path(tempfile.gettempdir())))

    def test_fail_open_branch_protects_against_unexpected_exception(self):
        # The kept `except Exception: return True` must still fire if host.py's
        # guard raises something other than ValueError (e.g. OSError from the
        # Windows ctypes drive probe). Verify it returns True, not raises.
        sys.path.insert(0, str(HOOKS_DIR))
        from _zmem_hook_common import assert_local_fs  # type: ignore
        import types

        class _BoomHost:
            @staticmethod
            def assert_local_fs(path):
                raise OSError("simulated drive probe failure")

        boom_mod = types.ModuleType("host")
        boom_mod.assert_local_fs = _BoomHost.assert_local_fs  # type: ignore
        with mock.patch.dict(sys.modules, {"host": boom_mod}):
            # Must return True (fail-open), not raise.
            result = assert_local_fs(Path(tempfile.gettempdir()))
            self.assertTrue(result)

    def test_valueerror_refusal_maps_to_false(self):
        # host.py's documented refusal signal (ValueError) MUST map to False.
        sys.path.insert(0, str(HOOKS_DIR))
        from _zmem_hook_common import assert_local_fs  # type: ignore
        import types

        class _RefusingHost:
            @staticmethod
            def assert_local_fs(path):
                raise ValueError("refusing network path")

        refuse_mod = types.ModuleType("host")
        refuse_mod.assert_local_fs = _RefusingHost.assert_local_fs  # type: ignore
        with mock.patch.dict(sys.modules, {"host": refuse_mod}):
            result = assert_local_fs(Path(tempfile.gettempdir()))
            self.assertFalse(result)


# ---------------------------------------------------------------------------
# L7/L8 — Hermes _tool_add validation (constants sourced from schema_meta)
# ---------------------------------------------------------------------------
class L7L8HermesValidation(unittest.TestCase):
    """The Hermes provider's _tool_add validates signal + content cap. The
    provider class inherits from the host-provided MemoryProvider base, so we
    can't instantiate it directly; instead we verify (a) the constants load
    correctly from schema_meta and (b) the validation message construction
    matches the MCP path by exercising _tool_add on a stubbed instance."""

    def test_store_constants_load_from_schema_meta(self):
        sys.path.insert(0, str(HERMES_DIR))
        # The provider module needs agent.memory_provider; stub it.
        import types
        agent_pkg = types.ModuleType("agent")
        mvp_mod = types.ModuleType("agent.memory_provider")
        class _MP:  # minimal base stand-in
            pass
        mvp_mod.MemoryProvider = _MP
        agent_pkg.memory_provider = mvp_mod
        with mock.patch.dict(sys.modules, {"agent": agent_pkg,
                                           "agent.memory_provider": mvp_mod}):
            import importlib
            for m in list(sys.modules):
                if m == "zmem_hermes_prov":
                    del sys.modules[m]
            spec = importlib.util.spec_from_file_location(
                "zmem_hermes_prov", str(HERMES_DIR / "__init__.py"))
            prov = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(prov)
            consts = prov._store_constants()
            self.assertIn("test", consts["ALLOWED_SIGNALS"])
            self.assertIn("none", consts["ALLOWED_SIGNALS"])
            self.assertIn("fact", consts["ALLOWED_TYPES"])
            self.assertEqual(consts["MAX_CONTENT_CHARS"], 65536)

    def test_tool_add_rejects_invalid_signal_and_oversize_content(self):
        sys.path.insert(0, str(HERMES_DIR))
        import types
        agent_pkg = types.ModuleType("agent")
        mvp_mod = types.ModuleType("agent.memory_provider")

        class _MP:  # minimal base stand-in
            pass
        mvp_mod.MemoryProvider = _MP
        agent_pkg.memory_provider = mvp_mod
        with mock.patch.dict(sys.modules, {"agent": agent_pkg,
                                           "agent.memory_provider": mvp_mod}):
            import importlib
            for m in list(sys.modules):
                if m == "zmem_hermes_prov2":
                    del sys.modules[m]
            spec = importlib.util.spec_from_file_location(
                "zmem_hermes_prov2", str(HERMES_DIR / "__init__.py"))
            prov = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(prov)
            # Build an instance without calling __init__ (the real init needs a
            # host session). _tool_add only reads self._namespace/_session_id,
            # which default on the stand-in.
            inst = prov.ZmemMemoryProvider.__new__(prov.ZmemMemoryProvider)
            inst._namespace = "project:l7l8"
            inst._session_id = ""
            # L7: invalid signal -> clean message naming the allowed set.
            r = json.loads(inst._tool_add({
                "type": "fact", "content": "ok", "signal": "bogus"}))
            self.assertIn("error", r)
            self.assertIn("signal must be one of", r["error"])
            # L8: oversize content -> clean message with the cap.
            r2 = json.loads(inst._tool_add({
                "type": "fact", "content": "x" * 70000, "signal": "test"}))
            self.assertIn("error", r2)
            self.assertIn("over the 65536 limit", r2["error"])
            # Invalid type is still rejected too.
            r3 = json.loads(inst._tool_add({
                "type": "nope", "content": "ok", "signal": "test"}))
            self.assertIn("error", r3)
            self.assertIn("type must be one of", r3["error"])


# `mcp_server` transitively imports `mcp` (via auth.py), which is NOT installed
# in CI (see tests/test_mcp_server.py for the same guard). Tests that import
# mcp_server must skip when mcp is unavailable — otherwise the test file crashes
# at collection on a clean CI runner.
import importlib.util
MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(MCP_AVAILABLE,
                     "mcp package not installed (MCP server tests need it)")
class L7L8McpImportNoSysExit(unittest.TestCase):
    """The MCP server's _load_store_constants() runs at import time. It locates
    schema_meta DIRECTLY via the in-tree path and must NOT call
    _resolve_zmem_home() (which writes a fatal-looking stderr message and
    sys.exit(2)s on a missing checkout). Importing the module outside a checkout
    (e.g. a lint pass or a test reading a constant) must be side-effect-free:
    no sys.exit, no stderr noise, just the module-level default constants
    (PRR-009)."""

    def test_load_store_constants_does_not_call_resolve_zmem_home(self):
        sys.path.insert(0, str(HERMES_DIR / "server"))
        import mcp_server  # type: ignore
        import unittest.mock
        # The loader must not touch _resolve_zmem_home at all. Use
        # assert_not_called (NOT a side_effect that the old `except Exception`
        # would have swallowed — that would be non-discriminating test theater).
        # This is the discriminating guard for PRR-009: the pre-fix loader
        # called _resolve_zmem_home(), so it would fail assert_not_called.
        with unittest.mock.patch.object(mcp_server, "_resolve_zmem_home") as mock_resolver:
            mcp_server._load_store_constants()
            mock_resolver.assert_not_called()


class L22BgLogShellLogic(unittest.TestCase):
    """L22: the BG_SINK fallback must use a strict conjunction so a
    missing/unwritable DATA_DIR falls through to /dev/null, never redirects
    into a path that doesn't exist (which would silently drop bg output)."""

    def _bg_sink_block(self):
        """Extract the BG_SINK assignment block from the hook (up to the
        maintenance dispatch line)."""
        src = (REPO_ROOT / "hooks" / "zmem-session-start.sh").read_text(
            encoding="utf-8")
        idx = src.index("BG_SINK=")
        end = src.index('"$PYTHON_BIN"', idx)
        return src[idx:end]

    def test_no_or_in_bg_sink_condition(self):
        block = self._bg_sink_block()
        # The buggy form was `mkdir -p ... && [ -w ... ] || [ -w ... ]`. After
        # the fix it must be a pure `&&` chain with no `||` fallback inside the
        # BG_SINK condition.
        bg_if = block[block.index("if mkdir"):]
        cond_end = bg_if.index("; then")
        cond = bg_if[:cond_end]
        self.assertNotIn("||", cond,
                         f"BG_SINK condition must not contain `||` (operator "
                         f"precedence bug): {cond!r}")

    @unittest.skipUnless(POSIX_BASH,
                         "behavioral shell test needs a working POSIX bash on PATH")
    def test_bg_sink_behavioral_writable_dir_and_optout(self):
        # PRR-012: behavioral test of the actual BG_SINK shell logic (not just a
        # source-text grep). Extracts the BG_SINK block from the hook and runs it
        # in a real bash subshell against a temp DATA_DIR, then asserts:
        # (a) a writable dir+file -> BG_SINK points at the log path;
        # (b) ZMEM_BG_LOG=0 -> BG_SINK is /dev/null (opt-out works).
        import tempfile, shutil
        block = self._bg_sink_block()

        def _run_block(data_dir, extra_env=""):
            # Normalize Windows backslashes to forward-slashes for bash.
            data_dir_bash = data_dir.replace("\\", "/")
            script = f'{extra_env}\nDATA_DIR="{data_dir_bash}"\n{block}\necho "SINK=$BG_SINK"'
            r = subprocess.run(["bash", "-c", script],
                               capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                if line.startswith("SINK="):
                    return line[len("SINK="):]
            return None
        tmp = tempfile.mkdtemp(prefix="zmem-l22-")
        try:
            # (a) writable dir -> log path
            sink = _run_block(tmp)
            expected = (tmp + "/zmem-bg.log").replace("\\", "/")
            self.assertEqual(sink, expected,
                             f"writable dir should yield log path, got {sink!r}")
            # (b) ZMEM_BG_LOG=0 -> /dev/null
            sink = _run_block(tmp, 'export ZMEM_BG_LOG=0')
            self.assertEqual(sink, "/dev/null",
                             f"ZMEM_BG_LOG=0 should yield /dev/null, got {sink!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(POSIX_BASH,
                         "behavioral shell test needs a working POSIX bash on PATH")
    def test_bg_sink_falls_back_when_log_file_readonly(self):
        # PRR-004: a read-only existing zmem-bg.log must fall back to /dev/null
        # (the file-writability probe fails), not silently drop maintenance.
        import tempfile, shutil, stat
        block = self._bg_sink_block()
        tmp = tempfile.mkdtemp(prefix="zmem-l22ro-")
        log = Path(tmp) / "zmem-bg.log"
        try:
            log.write_text("preexisting\n")
            os.chmod(str(log), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # read-only
            tmp_bash = tmp.replace("\\", "/")
            script = f'DATA_DIR="{tmp_bash}"\n{block}\necho "SINK=$BG_SINK"'
            r = subprocess.run(["bash", "-c", script],
                               capture_output=True, text=True, timeout=10)
            # The probe must NOT leak a "Permission denied" to stderr (the
            # `2>/dev/null` must apply before the `>>` redirect — PRR-004).
            self.assertEqual(r.stderr.strip(), "",
                             f"read-only probe leaked stderr noise: {r.stderr!r}")
            for line in r.stdout.splitlines():
                if line.startswith("SINK="):
                    self.assertEqual(line[len("SINK="):], "/dev/null",
                                     "read-only log file should fall back to /dev/null")
                    return
            self.fail("no SINK= line emitted")
        finally:
            # restore writability so rmtree can clean up
            try:
                os.chmod(str(log), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            shutil.rmtree(tmp, ignore_errors=True)


class L4IngestHarvestConstantsSourced(unittest.TestCase):
    """The 4th hard-coded copy of ALLOWED_TYPES/ALLOWED_SIGNALS/MAX_CONTENT_CHARS
    (in scripts/ingest_harvest.py) must now import from schema_meta so the drift
    class closed by L7/L8 is fully closed (no remaining copy can diverge)."""

    def test_ingest_harvest_imports_constants_from_schema_meta(self):
        src = (REPO_ROOT / "scripts" / "ingest_harvest.py").read_text(
            encoding="utf-8")
        # Must import from schema_meta, not re-type the literals as the primary
        # definition (a defensive fallback in an `except ImportError` is OK).
        self.assertIn("from schema_meta import", src)
        self.assertIn("ALLOWED_TYPES", src)
        self.assertIn("ALLOWED_SIGNALS", src)
        self.assertIn("MAX_CONTENT_CHARS", src)
        # The top-level re-definition (the pre-fix primary) must be gone — only
        # the fallback inside `except ImportError` may retain literals.
        import ast
        tree = ast.parse(src)
        top_assigns = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in (
                            "ALLOWED_TYPES", "ALLOWED_SIGNALS", "MAX_CONTENT_CHARS"):
                        top_assigns.append(t.id)
        self.assertEqual(top_assigns, [],
                         f"ingest_harvest.py still assigns constants at module "
                         f"top level: {top_assigns}")

    def test_ingest_harvest_constants_match_schema_meta(self):
        # Functional check: the validator uses the SAME values as schema_meta.
        sys.path.insert(0, str(SCRIPTS_DIR))
        import schema_meta  # type: ignore
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import ingest_harvest  # type: ignore
        self.assertEqual(ingest_harvest.ALLOWED_TYPES, schema_meta.ALLOWED_TYPES)
        self.assertEqual(ingest_harvest.ALLOWED_SIGNALS, schema_meta.ALLOWED_SIGNALS)
        self.assertEqual(ingest_harvest.MAX_CONTENT_CHARS, schema_meta.MAX_CONTENT_CHARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
