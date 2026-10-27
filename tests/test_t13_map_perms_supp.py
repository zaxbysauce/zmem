"""Supplementary pins for the #406 principal-map permission surface.

Non-frozen companions to the frozen driver tests/test_t12_map_perms.py:
auth/doctor helper parity, mtime-preserving drift detection in maybe_reload,
fstat wiring on both load paths, corrupt-rewrite fail-soft preservation,
owner-only loads, the Windows real-DACL trio (owner-only pass, Everyone-ACE
refusal, inspection-error refusal), and the per-request verify_token
refusal. Runs standalone (python tests/test_t13_map_perms_supp.py) and
under pytest.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import stat as _stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _map_owner_only(path) -> None:
    """#406 contract: a test map must not depend on the directory's
    inherited ACL (non-system drives inherit Users/Authenticated-Users
    allow ACEs, which the hub correctly refuses). POSIX uses chmod."""
    if os.name == "nt":
        subprocess.run(
            ["icacls", os.fspath(path), "/inheritance:r",
             "/grant", os.environ.get("USERNAME", "") + ":(F)"],
            capture_output=True, timeout=30)
    else:
        os.chmod(os.fspath(path), 0o600)


REPO_ROOT = Path(__file__).resolve().parents[1]
AUTH_PY = REPO_ROOT / "hermes-plugin" / "server" / "auth.py"
DOCTOR_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "doctor.py"

MAP = {
    "supp-secret-token-1": {
        "principal": "machine-supp",
        "host": "host-supp",
        "harness": "claude",
        "namespaces": ["project:supp"],
    }
}


def _load(name, path):
    if name not in sys.modules:
        try:
            import mcp  # noqa: F401
        except Exception:
            import types
            provider = types.ModuleType("mcp.server.auth.provider")

            class _AccessToken:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            class _TokenVerifier:
                pass

            provider.AccessToken = _AccessToken
            provider.TokenVerifier = _TokenVerifier
            m = types.ModuleType("mcp")
            s = types.ModuleType("mcp.server")
            a = types.ModuleType("mcp.server.auth")
            a.provider = provider
            s.auth = a
            m.server = s
            sys.modules.update({"mcp": m, "mcp.server": s,
                                "mcp.server.auth": a,
                                "mcp.server.auth.provider": provider})
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


@contextlib.contextmanager
def _stat_mode_for(path, mode):
    """os.stat monkeypatch for exactly one path (mtime preserved)."""
    real_stat = os.stat
    target = os.fspath(path)

    def _patched(arg, *args, **kwargs):
        try:
            hit = os.fspath(arg) == target
        except TypeError:
            hit = False
        if not hit:
            return real_stat(arg, *args, **kwargs)
        real = real_stat(arg, *args, **kwargs)
        fields = list(tuple(real)[:10])
        while len(fields) < 10:
            fields.append(0)
        fields[0] = mode
        return os.stat_result(
            tuple(fields), {"st_mtime_ns": getattr(real, "st_mtime_ns", 0)})

    os.stat = _patched
    try:
        yield
    finally:
        os.stat = real_stat


@contextlib.contextmanager
def _fstat_mode_for(mode):
    """os.fstat monkeypatch reporting ``mode`` for every descriptor."""
    real_fstat = os.fstat

    def _patched(fd, *args, **kwargs):
        real = real_fstat(fd, *args, **kwargs)
        fields = list(tuple(real)[:10])
        while len(fields) < 10:
            fields.append(0)
        fields[0] = mode
        return os.stat_result(
            tuple(fields), {"st_mtime_ns": getattr(real, "st_mtime_ns", 0)})

    os.fstat = _patched
    try:
        yield
    finally:
        os.fstat = real_fstat


def _write_map(tmp, name="tokens.json"):
    p = Path(tmp) / name
    p.write_text(json.dumps(MAP), encoding="utf-8")
    _map_owner_only(p)
    return p


def _fake_st(mode):
    return os.stat_result(
        (mode, 0, 0, 0, 0, 0, 0, 0, 0, 0), {})


class HelperParityTest(unittest.TestCase):
    """auth.py and doctor.py copies answer identically (the parser-mirror
    contract this repo uses whenever doctor must not import server auth)."""

    CASES = [
        (0o100644, "mode 0644"),
        (0o100600, None),
        (0o100640, "mode 0640"),
        (0o100666, "mode 0666"),
        (0o100777, "mode 0777"),
    ]
    ACE_CASES = [
        ([("S-1-5-18", 0), ("S-1-5-32-544", 0)], None),
        ([("S-1-1-0", 0)], "broad allow ACE grants S-1-1-0"),
        ([("S-1-5-32-545", 0)], "broad allow ACE grants S-1-5-32-545"),
        ([("S-1-5-11", 0)], "broad allow ACE grants S-1-5-11"),
        ([("S-1-1-0", 1)], None),  # denied ACE never grants
        ([("S-1-5-21-1-2-3-4", 0)], None),  # machine-local SID: out of scope
    ]

    def test_mode_verdicts_match(self):
        auth = _load("zmem_t13_perms_auth", AUTH_PY)
        doctor = _load("zmem_t13_perms_doctor", DOCTOR_PY)
        for mode, _ in self.CASES:
            if (os.name == "nt"
                    and _stat.S_IMODE(mode) in {0o666, 0o444}):
                continue  # synthesized shapes defer to the DACL leg
            a = auth._map_mode_problem(_fake_st(mode))
            d = doctor._map_mode_problem(_fake_st(mode))
            self.assertEqual(a, d, f"mode {oct(mode)} diverged")
            if _ is not None:
                self.assertIsNotNone(a, f"mode {oct(mode)} must be a problem")

    def test_ace_policy_matches(self):
        auth = _load("zmem_t13_perms_auth", AUTH_PY)
        doctor = _load("zmem_t13_perms_doctor", DOCTOR_PY)
        for aces, expect in self.ACE_CASES:
            a = auth._nt_allow_ace_problem("S-1-5-21-9-9-9-1", list(aces))
            d = doctor._nt_allow_ace_problem("S-1-5-21-9-9-9-1", list(aces))
            self.assertEqual(a, d, f"aces {aces} diverged")
            if expect is None:
                self.assertIsNone(a)
            else:
                self.assertIn(expect, a or "")


class RefusalBehaviorTest(unittest.TestCase):
    """Load/reload permission refusals and the preserved fail-soft class."""

    def _auth(self):
        return _load("zmem_t13_perms_auth", AUTH_PY)

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-t13-perms-")
        self._saved = {k: os.environ.get(k) for k in (
            "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE")}
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_owner_only_loads_and_serves(self):
        auth = self._auth()
        p = _write_map(self._tmp)
        pm = auth.load_principal_map(str(p))
        self.assertIsNotNone(pm.lookup_token("supp-secret-token-1"))

    def test_load_fstat_wiring_refuses_drifted_handle(self):
        """The AUTHORITATIVE check runs on the OPEN fd, not just the path."""
        import io
        auth = self._auth()
        p = _write_map(self._tmp)
        err = io.StringIO()
        with _fstat_mode_for(0o100644),                 contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                auth.load_principal_map(str(p))
        self.assertNotIn(ctx.exception.code, (None, 0))
        self.assertIn("644", err.getvalue())

    def test_reload_fstat_wiring_refuses_drifted_handle(self):
        auth = self._auth()
        p = _write_map(self._tmp)
        pm = auth.load_principal_map(str(p))
        with _fstat_mode_for(0o100644):
            with self.assertRaises(SystemExit):
                pm.force_reload()

    def test_maybe_reload_catches_mtime_preserving_drift(self):
        """chmod changes ctime, not mtime — the once-per-second stat still
        re-checks the permission shape (issue #406 claim 2)."""
        auth = self._auth()
        p = _write_map(self._tmp)
        pm = auth.load_principal_map(str(p))
        # The 1s reload rate-limit gate precedes the stat; move past it.
        import time
        time.sleep(1.1)
        with _stat_mode_for(str(p), 0o100644):  # mtime preserved by the patch
            with self.assertRaises(SystemExit) as ctx:
                pm.maybe_reload()
        self.assertIsInstance(ctx.exception, auth.MapPermissionRefused)

    def test_permission_refusal_class_not_softened(self):
        auth = self._auth()
        self.assertTrue(issubclass(auth.MapPermissionRefused, SystemExit))
        exc = auth.MapPermissionRefused("probe")
        self.assertEqual(exc.code, 2)

    def test_corrupt_rewrite_fail_soft_preserved(self):
        """A CORRUPT rewrite still keeps the previous map (no raise) — the
        #406 change must not have widened the fail-closed class."""
        auth = self._auth()
        p = _write_map(self._tmp)
        pm = auth.load_principal_map(str(p))
        p.write_text("{not valid json", encoding="utf-8")
        os.chmod(p, 0o600)
        changed = pm.force_reload()
        self.assertFalse(changed)
        self.assertIsNotNone(pm.lookup_token("supp-secret-token-1"))


@unittest.skipUnless(os.name == "nt", "real-DACL legs are Windows-only")
class RealDaclTest(unittest.TestCase):
    """The real Win32 wiring (handle conversion included), via icacls."""

    def _auth(self):
        return _load("zmem_t13_perms_auth", AUTH_PY)

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-t13-dacl-")
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_owner_only_temp_file_loads(self):
        auth = self._auth()
        p = _write_map(self._tmp)
        pm = auth.load_principal_map(str(p))  # exercises get_osfhandle path
        self.assertIsNotNone(pm.lookup_token("supp-secret-token-1"))

    def test_everyone_ace_refuses(self):
        auth = self._auth()
        p = _write_map(self._tmp)
        r = subprocess.run(
            ["icacls", str(p), "/grant", "Everyone:(R)"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        try:
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    auth.load_principal_map(str(p))
            self.assertIn("S-1-1-0", err.getvalue())
        finally:
            subprocess.run(["icacls", str(p), "/remove", "Everyone"],
                           capture_output=True, timeout=30)

    def test_inspection_error_refuses_fail_closed(self):
        auth = self._auth()
        p = _write_map(self._tmp)
        real = auth._nt_security_ace_tuples

        def _boom(path, fd):
            raise OSError("probe-induced inspection failure")

        auth._nt_security_ace_tuples = _boom
        try:
            import io
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    auth.load_principal_map(str(p))
            self.assertIn("could not inspect", err.getvalue())
        finally:
            auth._nt_security_ace_tuples = real


class PerRequestRefusalTest(unittest.TestCase):
    """The refusal escapes verify_token as process termination (round-2
    plan-critic finding R2-F5) — proven in a subprocess so the SystemExit
    cannot be absorbed by the test runner."""

    CHILD = r'''
import contextlib, importlib.util, json, os, sys, tempfile
spec = importlib.util.spec_from_file_location("authchild", sys.argv[1])
auth = importlib.util.module_from_spec(spec)
sys.modules["authchild"] = auth
spec.loader.exec_module(auth)
tmp = tempfile.mkdtemp(prefix="t13-verify-")
p = os.path.join(tmp, "tokens.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump({"supp-secret-token-1": {
        "principal": "m", "host": "h", "harness": "claude",
        "namespaces": ["project:supp"]}}, f)
os.chmod(p, 0o600)
pm = auth.load_principal_map(p)
verifier = auth.MapTokenVerifier(pm)
real_stat = os.stat

def _patched(arg, *a, **kw):
    try:
        hit = os.fspath(arg) == p
    except TypeError:
        hit = False
    if not hit:
        return real_stat(arg, *a, **kw)
    real = real_stat(arg, *a, **kw)
    fields = list(tuple(real)[:10])
    fields[0] = 0o100644
    return os.stat_result(tuple(fields), {"st_mtime_ns": real.st_mtime_ns})

os.stat = _patched  # drift, mtime unchanged
import asyncio, time
time.sleep(1.1)  # move past the 1s maybe_reload rate-limit gate
asyncio.run(verifier.verify_token("supp-secret-token-1"))
print("UNREACHABLE: verify_token returned after permission drift")
'''

    def test_verify_token_drift_terminates_nonzero(self):
        import time
        child = Path(self._tmp_child())
        child.write_text(self.CHILD, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(child), str(AUTH_PY)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, cwd=str(REPO_ROOT))
        self.assertNotEqual(r.returncode, 0,
                            f"verify_token must terminate nonzero on drift: "
                            f"{r.stdout} {r.stderr}")
        self.assertNotIn("UNREACHABLE", r.stdout)
        self.assertIn("zmem_mcp_principal_map_permission_refused", r.stderr)

    def _tmp_child(self):
        d = tempfile.mkdtemp(prefix="zmem-t13-verify-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, True))
        return str(Path(d) / "child.py")


@unittest.skipUnless(hasattr(__import__("signal"), "SIGHUP"),
                     "SIGHUP wiring is POSIX-only")
class SighupRefusalTest(unittest.TestCase):
    """SIGHUP-driven reload of a drifted map terminates the process nonzero
    with the refusal text (plan R3 lifecycle leg)."""

    CHILD_TEMPLATE = r'''
import importlib.util, json, os, signal, sys, tempfile, time
spec = importlib.util.spec_from_file_location("authchild", sys.argv[1])
auth = importlib.util.module_from_spec(spec)
sys.modules["authchild"] = auth
spec.loader.exec_module(auth)
tmp = tempfile.mkdtemp(prefix="t13-sighup-")
p = os.path.join(tmp, "tokens.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump({"supp-secret-token-1": {
        "principal": "m", "host": "h", "harness": "claude",
        "namespaces": ["project:supp"]}}, f)
os.chmod(p, 0o600)
pm = auth.load_principal_map(p)
signal.signal(signal.SIGHUP, lambda s, f: pm.force_reload())
real_stat = os.stat

def _patched(arg, *a, **kw):
    try:
        hit = os.fspath(arg) == p
    except TypeError:
        hit = False
    if not hit:
        return real_stat(arg, *a, **kw)
    real = real_stat(arg, *a, **kw)
    fields = list(tuple(real)[:10])
    fields[0] = 0o100644
    return os.stat_result(tuple(fields), {"st_mtime_ns": real.st_mtime_ns})

os.stat = _patched
os.kill(os.getpid(), signal.SIGHUP)
time.sleep(5)
print("UNREACHABLE: survived SIGHUP reload of a drifted map")
'''

    def test_sighup_drift_terminates_nonzero(self):
        child = Path(tempfile.mkdtemp(prefix="zmem-t13-sighup-")) / "child.py"
        child.write_text(self.CHILD_TEMPLATE, encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-X", "utf8", str(child), str(AUTH_PY)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, cwd=str(REPO_ROOT))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("UNREACHABLE", r.stdout)
        self.assertIn("zmem_mcp_principal_map_permission_refused", r.stderr)


if __name__ == "__main__":
    unittest.main()
