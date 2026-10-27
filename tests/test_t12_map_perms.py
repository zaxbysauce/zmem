"""Principal-map PERMISSION-shape verdicts (issues #406 AC1 + AC2): driver.

Issue #406's own check design: a DRIVER script with two legs, plus the same
assertions carried as pytest TestCase methods so `pytest` can run them after
the fix. Both legs are NEW-SURFACE — at base no permission verdict exists
anywhere on the map surface, so the driver prints a NOFAIL sentinel and
exits 1; after the fix it exits 0 only when the verdict/refusal is real.

Leg 1 (AC1, default argv): build a one-entry principal map (the exact JSON
shape of trace probe P4), monkeypatch os.stat for that path so st_mode
reports 0o100644 (group+world readable) deterministically on ANY platform
(never a real chmod), then invoke doctor's REAL fleet-principal lane for
that map (skills/memory/scripts/doctor.py::_check_fleet_principal — the
function behind `doctor --fleet`'s fleet-principal check, plural-map lane).
- base: the lane reports content-only (pass "Principal map valid"), no
  permission shape/mode mention anywhere in its result -> print exactly
  `NOFAIL: no map-permission verdict exists at base`, exit 1.
- fix: the lane must produce a map-permission verdict; the driver asserts
  it is a FAIL naming the offending permission shape, exit 0. A verdict
  that exists but passes the 0o100644 map is a POSTFIX-MISMATCH (exit 1).

Leg 2 (AC2, `--hub-refusal` argv): spec-load hermes-plugin/server/auth.py.
- 2a: load_principal_map(path) with os.stat monkeypatched to 0o100644.
  base: returns a PrincipalMap (serves the map) -> print exactly
  `NOFAIL: hub accepts an over-permissive map at base`, exit 1.
  fix: must refuse — SystemExit nonzero with stderr/stdout naming the file
  and the mode.
- 2b (same driver run): load under a correctly-permissioned 0o100600, then
  flip the monkeypatch to 0o100644 and call force_reload(). base: returns
  True/False without refusal (same sentinel, exit 1). fix: the reload must
  refuse (SystemExit nonzero, or an explicit refusal) rather than swap in /
  keep serving silently.

SystemExit is handled cleanly in the driver so a post-fix refusal exit is
the CHECK's verdict, never an accidental traceback. The doctor/auth
modules are imported inside functions (never at module top) so this file
imports cleanly at base on every host.

Permission-verdict vocabulary the fix's verdict/refusal text is expected to
use (any one suffices): permission, permissive, group, world, icacls,
readable, chmod, st_mode, s_imode, allow ace, 0o644, or any 0oNNN octal.

Runs standalone: python tests/test_t12_map_perms.py [--hub-refusal]
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "doctor.py"
AUTH_PY = REPO_ROOT / "hermes-plugin" / "server" / "auth.py"

# Deterministic permission inputs (S_IFREG | mode). The verdict input is
# ALWAYS the monkeypatched stat — never a real chmod, so behavior is
# identical on POSIX and Windows.
OVERPERMISSIVE_MODE = 0o100644  # rw-r--r-- : group+world readable
OWNER_ONLY_MODE = 0o100600      # rw------- : the installer contract

# Terms whose presence in a doctor-lane result (or an auth refusal message)
# means "this is a map-permission verdict/refusal". See module docstring.
PERMISSION_VERDICT_TERMS = (
    "permission", "permissive", "group", "world", "icacls", "readable",
    "chmod", "st_mode", "s_imode", "allow ace", "0o644",
)
_OCTAL_MODE_RE = re.compile(r"0o[0-7]{3,4}")

# The one-entry map shape validated by trace probe P4 (content-valid, so at
# base the doctor lane passes on CONTENT and only the permission dimension
# can fail).
MAP_TEMPLATE = {
    "probe-secret-token-1": {
        "principal": "machine-a",
        "host": "host-a",
        "harness": "claude",
        "namespaces": ["project:probe"],
    },
}

_SENTINEL_DOCTOR = "NOFAIL: no map-permission verdict exists at base"
_SENTINEL_HUB = "NOFAIL: hub accepts an over-permissive map at base"


# -- os.stat monkeypatch --------------------------------------------------

@contextlib.contextmanager
def _stat_mode_for(path, mode):
    """Monkeypatch os.stat so ONLY this exact path reports ``mode``.

    Saves the real os.stat, wraps it so calls whose argument (str or Path,
    compared via os.fspath) is exactly ``path`` get a real os.stat_result
    with st_mode replaced (st_mtime_ns preserved — the auth reload path
    reads it), and restores the real function in finally. Path.stat()
    delegates to os.stat, so both call shapes are covered.
    """
    real_stat = os.stat
    target = os.fspath(path)

    def _patched(arg, *args, **kwargs):
        try:
            hit = os.fspath(arg) == target
        except TypeError:  # e.g. an fd int — never our path
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


def _mentions_permission(text):
    lowered = (text or "").lower()
    if any(term in lowered for term in PERMISSION_VERDICT_TERMS):
        return True
    return bool(_OCTAL_MODE_RE.search(lowered))


# -- production-module loaders (inside functions: file imports clean at base)

def _load_doctor():
    name = "zmem_t12_doctor"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, DOCTOR_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _load_auth():
    name = "zmem_t12_auth"
    if name not in sys.modules:
        try:
            import mcp  # noqa: F401
        except Exception:
            # Minimal stub for mcp.server.auth.provider when the mcp package
            # is absent, so auth.py execs identically with or without it
            # (same pattern as tests/test_t02_principal.py).
            import types
            provider = types.ModuleType("mcp.server.auth.provider")

            class _AccessToken:  # attribute bag, matching the SDK shape
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class _TokenVerifier:  # Protocol stand-in
                pass

            provider.AccessToken = _AccessToken
            provider.TokenVerifier = _TokenVerifier
            mcp_mod = types.ModuleType("mcp")
            server_mod = types.ModuleType("mcp.server")
            auth_ns = types.ModuleType("mcp.server.auth")
            auth_ns.provider = provider
            server_mod.auth = auth_ns
            mcp_mod.server = server_mod
            sys.modules.update({
                "mcp": mcp_mod, "mcp.server": server_mod,
                "mcp.server.auth": auth_ns,
                "mcp.server.auth.provider": provider,
            })
        spec = importlib.util.spec_from_file_location(name, AUTH_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _write_map(tmp):
    map_path = Path(tmp) / "tokens.json"
    map_path.write_text(json.dumps(MAP_TEMPLATE), encoding="utf-8")
    # CHECK_WRONG amend (2nd): the fix's REAL fstat/DACL verdict consults
    # the open descriptor, which the os.stat monkeypatch cannot drive — on
    # POSIX an umask-022 map (0o644) refused at leg 2b's initial load, and
    # on Windows a map written under a broad-ACL directory (non-system
    # drives inherit Users/Authenticated-Users allow ACEs) refused at
    # startup. Pin the installer contract for REAL so the monkeypatched
    # stat remains the only verdict input the driver varies.
    import subprocess
    if os.name == "nt":
        subprocess.run(
            ["icacls", str(map_path), "/inheritance:r",
             "/grant", os.environ.get("USERNAME", "") + ":(F)"],
            capture_output=True, timeout=30)
    else:
        os.chmod(map_path, 0o600)
    return map_path


# -- AC1: doctor fleet-principal lane --------------------------------------

def run_doctor_lane():
    """Call the REAL doctor fleet-principal lane for a 0o100644 map.

    Returns {result, verdict_present, matched_terms}. ``result`` is the
    check dict the lane produces ({"id": "fleet-principal", "status": ...,
    "summary": ..., "details": {...}}); ``verdict_present`` says whether it
    carries any map-permission verdict.
    """
    tmp = tempfile.mkdtemp(prefix="t12-doctor-")
    map_path = _write_map(tmp)
    saved = {
        "ZMEM_MCP_TOKENS_FILE": os.environ.get("ZMEM_MCP_TOKENS_FILE"),
        "ZMEM_MCP_TOKEN": os.environ.get("ZMEM_MCP_TOKEN"),
        "ZMEM_MCP_TOKEN_FILE": os.environ.get("ZMEM_MCP_TOKEN_FILE"),
    }
    os.environ["ZMEM_MCP_TOKENS_FILE"] = str(map_path)
    os.environ.pop("ZMEM_MCP_TOKEN", None)
    os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
    try:
        doctor = _load_doctor()
        # The plural-map lane ignores the hello tuple (it is only consumed
        # by the singular broker lane) — pass a successful hello shape.
        with _stat_mode_for(str(map_path), OVERPERMISSIVE_MODE):
            result = doctor._check_fleet_principal(("pass", "hello ok", {}))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)
    text = json.dumps(result, default=str)
    matched = [t for t in PERMISSION_VERDICT_TERMS if t in text.lower()]
    octal_named = bool(_OCTAL_MODE_RE.search(text.lower()))
    return {
        "result": result,
        "verdict_present": bool(matched or octal_named),
        "matched_terms": matched,
        "octal_named": octal_named,
    }


def main_doctor_lane() -> int:
    outcome = run_doctor_lane()
    result = outcome["result"]
    if not outcome["verdict_present"]:
        print(_SENTINEL_DOCTOR)
        return 1
    status = result.get("status")
    summary = str(result.get("summary", ""))
    if status == "fail":
        print(f"OK: fleet-principal verdict FAILS the over-permissive "
              f"map: {summary}")
        return 0
    print(f"POSTFIX-MISMATCH: map-permission verdict exists but status="
          f"{status!r} for a 0o100644 map: {summary}")
    return 1


# -- AC2: hub load + force_reload refusal ----------------------------------

def run_hub_refusal_legs():
    """Both AC2 legs against the REAL auth module. Returns an outcome dict
    {leg_load: {...}, leg_reload: {...}}; each leg carries ``base_behavior``
    (True when the base serves/keeps without refusal) plus refusal details.
    """
    auth = _load_auth()
    tmp = tempfile.mkdtemp(prefix="t12-hub-")
    try:
        map_path = _write_map(tmp)
        name = map_path.name

        # Leg 2a: load with the map group/world-readable.
        err = io.StringIO()
        served = None
        exit_exc = None
        with _stat_mode_for(str(map_path), OVERPERMISSIVE_MODE), \
                contextlib.redirect_stderr(err):
            try:
                served = auth.load_principal_map(str(map_path))
            except SystemExit as exc:
                exit_exc = exc
        text = " ".join([err.getvalue(), str(exit_exc or "")])
        leg_load = {
            "served": served is not None,
            "exit_code": getattr(exit_exc, "code", None),
            "text": text,
        }
        if served is not None:
            leg_load["base_behavior"] = True
        else:
            names_file = str(map_path) in text or name in text
            leg_load["refusal_named_file"] = names_file
            leg_load["refusal_named_mode"] = (
                "644" in text or _mentions_permission(text))
            leg_load["base_behavior"] = False

        # Leg 2b: load owner-only (0o600 — must succeed pre/post), then the
        # file's permission drifts to 0o100644 and force_reload() runs.
        err2 = io.StringIO()
        with _stat_mode_for(str(map_path), OWNER_ONLY_MODE):
            good = auth.load_principal_map(str(map_path))
        changed = None
        exit_exc2 = None
        with _stat_mode_for(str(map_path), OVERPERMISSIVE_MODE), \
                contextlib.redirect_stderr(err2):
            try:
                changed = good.force_reload()
            except SystemExit as exc:
                exit_exc2 = exc
        text2 = " ".join([err2.getvalue(), str(exit_exc2 or "")])
        leg_reload = {
            "changed": changed,
            "exit_code": getattr(exit_exc2, "code", None),
            "text": text2,
        }
        if exit_exc2 is None and isinstance(changed, bool):
            # No refusal raised; True = swapped in the over-permissive map,
            # False = kept serving the previous map. A refusal only counts
            # when the stderr text explicitly names the file and the
            # permission shape (an explicit, logged refusal — not a silent
            # keep).
            explicit = (str(map_path) in text2 or name in text2) and \
                _mentions_permission(text2)
            leg_reload["explicit_refusal"] = explicit
            leg_reload["base_behavior"] = not explicit
        else:
            leg_reload["base_behavior"] = False
        return {"leg_load": leg_load, "leg_reload": leg_reload}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main_hub_refusal() -> int:
    legs = run_hub_refusal_legs()
    base_like = sorted(k for k, v in legs.items() if v["base_behavior"])
    if base_like:
        print(_SENTINEL_HUB)
        return 1
    load_leg, reload_leg = legs["leg_load"], legs["leg_reload"]
    # Post-fix quality (AC2: exit nonzero, stderr naming file+mode).
    if not load_leg["exit_code"] or load_leg["exit_code"] in (0, "0"):
        print(f"POSTFIX-MISMATCH: load refusal exits {load_leg['exit_code']!r} "
              f"(must be nonzero): {load_leg['text'][:300]}")
        return 1
    if not (load_leg.get("refusal_named_file")
            and load_leg.get("refusal_named_mode")):
        print(f"POSTFIX-MISMATCH: load refusal does not name file+mode: "
              f"{load_leg['text'][:300]}")
        return 1
    refused_reload = (
        (reload_leg["exit_code"] not in (None, 0, "0"))
        or reload_leg.get("explicit_refusal"))
    if reload_leg.get("changed") is True or not refused_reload:
        print(f"POSTFIX-MISMATCH: reload did not refuse the over-permissive "
              f"map (changed={reload_leg.get('changed')!r}): "
              f"{reload_leg['text'][:300]}")
        return 1
    print("OK: hub load and force_reload both refuse the over-permissive "
          "map, naming the file and the permission shape.")
    return 0


# -- carried pytest assertions (same checks, class-qualified) --------------
# NEW-SURFACE semantics: at base these are vacuously green (no verdict /
# no refusal exists to judge — the __main__ driver reports the NOFAIL
# sentinel and exit 1); after the fix they assert the verdict/refusal
# quality and go red if the fix's verdict passes an over-permissive map.

class DoctorFleetPrincipalMapPermissionTest(unittest.TestCase):
    """AC1: doctor --fleet must FAIL a group/world-readable principal map."""

    def test_fleet_principal_names_overpermissive_map_mode(self):
        outcome = run_doctor_lane()
        if not outcome["verdict_present"]:
            # Base: no map-permission verdict exists (the driver prints the
            # NOFAIL sentinel). Nothing to assert against yet.
            return
        result = outcome["result"]
        self.assertEqual(
            result.get("status"), "fail",
            f"a 0o100644 principal map must FAIL the fleet-principal "
            f"check; got {result!r}")
        summary = str(result.get("summary", "")).lower()
        self.assertTrue(
            _mentions_permission(summary),
            f"the FAIL verdict must name the offending permission shape; "
            f"got summary {result.get('summary')!r}")


class HubMapPermissionRefusalTest(unittest.TestCase):
    """AC2: hub load and mtime reload must refuse an over-permissive map."""

    def test_load_and_reload_refuse_overpermissive_map(self):
        legs = run_hub_refusal_legs()
        base_like = sorted(k for k, v in legs.items() if v["base_behavior"])
        if base_like:
            # Base: the hub serves/keeps the map without refusal (the
            # driver prints the NOFAIL sentinel). Nothing to assert yet.
            return
        load_leg, reload_leg = legs["leg_load"], legs["leg_reload"]
        self.assertTrue(
            load_leg["exit_code"] not in (None, 0, "0"),
            f"load refusal must exit nonzero: {load_leg['text'][:300]}")
        self.assertTrue(load_leg.get("refusal_named_file"),
                        f"load refusal must name the file: "
                        f"{load_leg['text'][:300]}")
        self.assertTrue(load_leg.get("refusal_named_mode"),
                        f"load refusal must name the mode: "
                        f"{load_leg['text'][:300]}")
        self.assertFalse(reload_leg.get("changed") is True,
                          "force_reload must not swap in an "
                          "over-permissive map")
        self.assertTrue(
            reload_leg["exit_code"] not in (None, 0, "0")
            or reload_leg.get("explicit_refusal"),
            f"force_reload must refuse (raise or explicit refusal): "
            f"{reload_leg['text'][:300]}")


if __name__ == "__main__":
    if "--hub-refusal" in sys.argv[1:]:
        sys.exit(main_hub_refusal())
    sys.exit(main_doctor_lane())
