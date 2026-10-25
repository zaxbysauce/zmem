"""Supplementary pins for issue #291 (T-12 fleet ops) beyond the frozen set.

Covers the plan-REV-4 supplementary list: the three directory-variable limbs
(refusal + preservation + precedence), doctor's error-mode report under a
refused configuration, no-alternate-destination containment (correction
queue / storelib config / snapshot serve / broker envelope), non-creating
fleet path math, the hello classification taxonomy (stubbed shapes + a REAL
transport 401), the broker credential flow, the principal-map parity corpus
against hermes-plugin/server/auth.py's constants, issue-principal behavior
(success / duplicate / contention / preset / degraded lock), and installer
statics (no wildcard bind). Frozen drivers live in test_t12_fleet_ops.py;
this file adds hardening only and never touches production state.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
DOCTOR_PY = SCRIPTS_DIR / "doctor.py"
ISSUE_PRINCIPAL_PY = REPO_ROOT / "scripts" / "fleet" / "issue-principal.py"
HERMES_SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
PYTHON = sys.executable

sys.path.insert(0, str(REPO_ROOT / "tests"))
import test_t12_fleet_ops as T12  # noqa: E402  (shared frozen-file helpers)

STRIP_VARS = T12.STRIP_VARS
FLEET_CHECK_IDS = T12.FLEET_CHECK_IDS


def _run(argv, env, cwd=None, timeout=90):
    return subprocess.run(
        [PYTHON, "-X", "utf8"] + argv,
        cwd=str(cwd or REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=timeout)


def _py(code: str, env, timeout=90):
    return _run(["-c", code], env, timeout=timeout)


# ---------------------------------------------------------------------------
# (a) all three directory-variable limbs + precedence + passthrough
# ---------------------------------------------------------------------------


def test_directory_variable_limbs_refuse_file_shape():
    for var in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        with tempfile.TemporaryDirectory(prefix="zmem-t12s-limb-") as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            target = tmp / "not-a-dir.txt"
            target.write_text("file, not a data dir", encoding="utf-8")
            env = T12._base_env(home)
            env[var] = str(target)
            result = _run(
                [str(STORE_PY), "recent", "--limit", "1"], env)
            assert result.returncode == 2, (
                f"{var}: expected exit 2, rc={result.returncode} "
                f"stderr={result.stderr!r}")
            assert "ZMEM_STORE" in (result.stdout + result.stderr), (
                f"{var}: refusal must name ZMEM_STORE: {result.stderr!r}")
            assert "Traceback" not in result.stderr, result.stderr


def test_zmem_store_precedence_over_file_shaped_data():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-prec-") as td:
        tmp = Path(td)
        home = tmp / "home"
        home.mkdir()
        data_dir = tmp / "data"
        data_dir.mkdir()
        bogus = tmp / "a-file.txt"
        bogus.write_text("x", encoding="utf-8")
        store = data_dir / "store.sqlite"
        env = T12._base_env(home)
        env["ZMEM_DATA"] = str(bogus)          # file-shaped: would refuse
        env["ZMEM_STORE"] = str(store)          # hard override wins
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        result = _py("import host; print(host.resolve_store_path())", env)
        assert result.returncode == 0, result.stderr
        resolved = result.stdout.strip().splitlines()[-1].strip()
        assert os.path.normcase(resolved) == os.path.normcase(str(store))


def test_nonexistent_data_path_passes_through():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-new-") as td:
        tmp = Path(td)
        env = T12._base_env(tmp / "home")
        env["ZMEM_DATA"] = str(tmp / "not-yet-created")
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        result = _py("import host; print(host.resolve_store_path())", env)
        assert result.returncode == 0, result.stderr
        resolved = result.stdout.strip().splitlines()[-1].strip()
        assert os.path.normcase(resolved) == os.path.normcase(
            str(tmp / "not-yet-created" / "store.sqlite"))


# ---------------------------------------------------------------------------
# (d) doctor error-mode: full JSON report under each refused limb
# ---------------------------------------------------------------------------


def test_doctor_error_mode_emits_json_for_every_limb():
    for var in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        with tempfile.TemporaryDirectory(prefix="zmem-t12s-err-") as td:
            tmp = Path(td)
            target = tmp / "file-shaped.txt"
            target.write_text("nope", encoding="utf-8")
            env = T12._base_env(tmp / "home")
            env[var] = str(target)
            env["ZMEM_MODELS_DIR"] = str(tmp / "missing-models")
            env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
            result = _run([str(DOCTOR_PY), "--fleet", "--format", "json"], env)
            assert "Traceback" not in result.stderr, (var, result.stderr)
            assert result.stdout.strip(), (var, result.stderr[-300:])
            report = json.loads(result.stdout)
            ids = {c.get("id"): c.get("status") for c in report["checks"]}
            assert ids.get("store-resolution") == "fail", (var, ids)
            fleet = [i for i in ids if isinstance(i, str)
                     and i.startswith("fleet-")]
            missing = [i for i in FLEET_CHECK_IDS if i not in ids]
            assert not missing, (var, f"fleet checks missing: {missing}")
            assert len(fleet) == len(FLEET_CHECK_IDS), (var, ids)
            any_fail = any(s == "fail" for s in ids.values())
            assert (result.returncode == 0) == (not any_fail)


# ---------------------------------------------------------------------------
# (c) no-alternate-destination containment + broker fail-open
# ---------------------------------------------------------------------------


def test_correction_queue_and_config_raise_instead_of_fallback():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-fb-") as td:
        tmp = Path(td)
        target = tmp / "file-shaped.txt"
        target.write_text("nope", encoding="utf-8")
        env = T12._base_env(tmp / "home")
        env["ZMEM_DATA"] = str(target)
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        code = (
            "import os, sys\n"
            "import host\n"
            "import correction_queue\n"
            "try:\n"
            "    correction_queue.resolve_queue_dir()\n"
            "    print('QUEUE-FALLBACK')\n"
            "except host.StoreConfigError:\n"
            "    print('QUEUE-RAISED')\n"
        )
        result = _py(code, env)
        assert "QUEUE-RAISED" in result.stdout, (
            f"correction_queue must not fall back to ~/.zmem: "
            f"{result.stdout!r} {result.stderr!r}")
        code2 = (
            "import sys\n"
            "sys.path.insert(0, r'%s')\n"
            "try:\n"
            "    import storelib.config as c\n"
            "    c._store_dir()\n"
            "    print('CONFIG-CWD-FALLBACK')\n"
            "except Exception as exc:\n"
            "    print('CONFIG-' + type(exc).__name__)\n"
        ) % SCRIPTS_DIR
        result2 = _py(code2, env)
        # storelib refuses at IMPORT time (schema.py resolves the store at
        # module scope) — the same fail-loud class, one step earlier than
        # _store_dir's own guard; both are the no-fallback contract.
        assert "CONFIG-StoreConfigError" in result2.stdout, (
            f"storelib must refuse instead of falling back to the env "
            f"chain/cwd: {result2.stdout!r} {result2.stderr!r}")


def test_snapshot_serve_and_broker_fail_open_under_refusal():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-fo-") as td:
        tmp = Path(td)
        target = tmp / "file-shaped.txt"
        target.write_text("nope", encoding="utf-8")
        env = T12._base_env(tmp / "home")
        env["ZMEM_DATA"] = str(target)
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        env["ZMEM_REMOTE_URL"] = "http://127.0.0.1:1"
        code = (
            "from fleet import snapshot, broker\n"
            "env = dict(__import__('os').environ)\n"
            "print('SERVE', snapshot._serve_offline_read("
            "env, query='q', namespace='user:global', moment='user_prompt', "
            "session_id='s'))\n"
            "out = broker.handle_event('UserPromptSubmit', "
            "{'session_id': 's', 'prompt': 'q'}, env)\n"
            "print('ENVELOPE', isinstance(out, dict), "
            "'additionalContext' in out)\n"
        )
        result = _py(code, env, timeout=120)
        assert "SERVE (None, 'none')" in result.stdout, (
            f"snapshot serve must degrade to (None, none): "
            f"{result.stdout!r} {result.stderr!r}")
        assert "ENVELOPE True True" in result.stdout, (
            f"broker.handle_event must keep the fail-open envelope: "
            f"{result.stdout!r} {result.stderr!r}")


# ---------------------------------------------------------------------------
# (d/e) fleet path math is non-creating
# ---------------------------------------------------------------------------


def test_fleet_checks_create_no_directories():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-nocreate-") as td:
        tmp = Path(td)
        data_dir = tmp / "data"
        data_dir.mkdir()
        env = T12._base_env(tmp / "home")
        env["ZMEM_DATA"] = str(data_dir)
        env["ZMEM_MODELS_DIR"] = str(tmp / "missing-models")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        result = _run([str(DOCTOR_PY), "--fleet", "--format", "json"], env)
        assert "Traceback" not in result.stderr, result.stderr
        assert result.stdout.strip(), result.stderr[-300:]
        assert not (data_dir / "queue").exists(), "queue/ must not be created"
        assert not (data_dir / "broker").exists(), (
            "broker/ must not be created")


# ---------------------------------------------------------------------------
# (f) hello classification taxonomy
# ---------------------------------------------------------------------------

_STUB_CODE = """
import os, sys, types
sys.path.insert(0, {scripts!r})
fake = types.ModuleType('fleet')
fake_transport = types.ModuleType('fleet.transport')
class _Http:
    def __init__(self, **kw):
        pass
    def hello(self):
        return {payload}
fake_transport.McpHttp = _Http
fake_transport.DeadlineExecutor = lambda: None
fake_transport.CLIENT_PROTOCOL_MAJOR = 1
fake_transport.REASON_HUB_UNREACHABLE = 'hub-unreachable'
fake.transport = fake_transport
sys.modules['fleet'] = fake
sys.modules['fleet.transport'] = fake_transport
import doctor
v, s, d = doctor._fleet_hello_once()
print('VERDICT', v)
print('SUMMARY', s)
"""


def test_hello_classification_shapes():
    shapes = [
        # success payload: negotiation fields, NO ok member (the real hub)
        ({"protocol": 1, "supported_protocol_majors": [1]}, "pass"),
        # major skew: hub window excludes the client major
        ({"protocol": 2, "supported_protocol_majors": [2]}, "fail"),
        # connect failure collapse: skip (fleet-hub-reachable owns it)
        ({"ok": False, "reason": "hub-unreachable"}, "skip"),
        # the 401 collapse: reachable-but-unsuccessful IS a failure
        ({"ok": False, "reason": "empty-pool"}, "fail"),
    ]
    for payload, expected in shapes:
        with tempfile.TemporaryDirectory(prefix="zmem-t12s-hello-") as td:
            env = T12._base_env(Path(td) / "home")
            env["ZMEM_REMOTE_URL"] = "http://127.0.0.1:1"
            result = _py(_STUB_CODE.format(scripts=str(SCRIPTS_DIR),
                                            payload=repr(payload)), env)
            assert f"VERDICT {expected}" in result.stdout, (
                f"{payload} -> expected {expected}: "
                f"{result.stdout!r} {result.stderr!r}")


def test_hello_real_transport_401_is_a_failure():
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(401)
            self.end_headers()

        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="zmem-t12s-401-") as td:
            env = T12._base_env(Path(td) / "home")
            env["ZMEM_REMOTE_URL"] = f"http://127.0.0.1:{port}/mcp"
            code = (
                "import sys\n"
                "sys.path.insert(0, {scripts!r})\n"
                "import doctor\n"
                "v, s, d = doctor._fleet_hello_once()\n"
                "print('VERDICT', v)\n"
            ).format(scripts=str(SCRIPTS_DIR))
            result = _py(code, env, timeout=120)
            assert "VERDICT fail" in result.stdout, (
                f"a reachable hub refusing auth must be a hello FAILURE "
                f"(the transport collapses 401 to a non-unreachable "
                f"reason): {result.stdout!r} {result.stderr!r}")
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# (g) broker credential flow
# ---------------------------------------------------------------------------


def test_broker_credential_file_resolves_through_transport():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-cred-") as td:
        tmp = Path(td)
        token = "t12s-broker-token-rotate-me"
        token_file = tmp / "broker-token.json"
        token_file.write_text(
            json.dumps({"token": token, "namespaces": ["project:x"]}),
            encoding="utf-8")
        env = T12._base_env(tmp / "home")
        env["ZMEM_MCP_TOKEN_FILE"] = str(token_file)
        # plural map var deliberately ABSENT — the installed-broker shape
        code = (
            "import sys\n"
            "sys.path.insert(0, {scripts!r})\n"
            "from fleet import transport\n"
            "print('TOKEN', transport._resolve_token(None, dict("
            "__import__('os').environ)))\n"
        ).format(scripts=str(SCRIPTS_DIR))
        result = _py(code, env)
        assert f"TOKEN {token}" in result.stdout, (
            f"the singular token file must feed the client transport: "
            f"{result.stdout!r} {result.stderr!r}")


def test_malformed_token_file_fails_principal_lane():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-bad-") as td:
        tmp = Path(td)
        token_file = tmp / "broker-token.json"
        token_file.write_text("{not json", encoding="utf-8")
        env = T12._base_env(tmp / "home")
        env["ZMEM_MCP_TOKEN_FILE"] = str(token_file)
        code = (
            "import sys\n"
            "sys.path.insert(0, {scripts!r})\n"
            "import doctor\n"
            "check = doctor._check_fleet_principal(('skip', 'unreachable', {{}}))\n"
            "print('STATUS', check['status'])\n"
        ).format(scripts=str(SCRIPTS_DIR))
        result = _py(code, env)
        assert "STATUS fail" in result.stdout, (
            f"a malformed broker token file must fail the principal lane "
            f"even when the hub is unreachable: {result.stdout!r} "
            f"{result.stderr!r}")


# ---------------------------------------------------------------------------
# (h) principal-map parity corpus (constants imported from server auth)
# ---------------------------------------------------------------------------


def test_principal_map_parity_corpus():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-par-") as td:
        env = T12._base_env(Path(td) / "home")
        code = (
            "import sys\n"
            "sys.path.insert(0, {scripts!r})\n"
            "sys.path.insert(0, {auth!r})\n"
            "import doctor\n"
            "try:\n"
            "    import auth as server_auth\n"
            "except Exception as exc:\n"
            "    print('AUTH-IMPORT-FAILED', type(exc).__name__)\n"
            "else:\n"
            "    print('FIELDS-EQ', set(doctor._FLEET_PRINCIPAL_ALLOWED_FIELDS)"
            " == set(server_auth._PRINCIPAL_ENTRY_FIELDS))\n"
            "    print('MAXP-EQ', doctor._FLEET_MAX_PRINCIPALS == "
            "server_auth._MAX_PRINCIPALS)\n"
            "    print('NS-EQ', doctor._FLEET_MAX_NAMESPACES == "
            "server_auth._MAX_TOKEN_NAMESPACES, "
            "doctor._FLEET_MAX_NS_UTF8_BYTES == "
            "server_auth._MAX_TOKEN_NAMESPACE_UTF8_BYTES, "
            "doctor._FLEET_MAX_NS_TOTAL_UTF8_BYTES == "
            "server_auth._MAX_TOKEN_NAMESPACES_TOTAL_UTF8_BYTES)\n"
        ).format(scripts=str(SCRIPTS_DIR), auth=str(HERMES_SERVER_DIR))
        result = _py(code, env)
        out = result.stdout
        if "AUTH-IMPORT-FAILED" in out:
            import pytest
            pytest.skip(f"server auth import unavailable (mcp dep?): {out}")
        assert "FIELDS-EQ True" in out, out
        assert "MAXP-EQ True" in out, out
        assert "NS-EQ True True True" in out, out

    # The corpus (pure function; deterministic):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import doctor
    finally:
        sys.path.remove(str(SCRIPTS_DIR))

    def problems_for(entries):
        return doctor._fleet_principal_map_problems(json.dumps(entries))[0]

    good = {"tok-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1": {
        "principal": "p1", "host": "h", "harness": "claude-code"}}
    assert problems_for(good) == []
    # POSITIVE: a fixture WITHOUT revoked passes; optional fields accepted
    good2 = {"tok-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2": {
        "principal": "p2", "host": "h", "harness": "codex",
        "namespaces": ["user:global"], "capabilities": ["read", "passive"],
        "revoked": False}}
    assert problems_for(good2) == []
    # NEGATIVE corpus
    assert any("revoked" in p for p in problems_for(
        {"t1": {"principal": "p", "host": "h", "harness": "x",
                "revoked": "yes"}}))
    assert any("unknown field" in p for p in problems_for(
        {"t1": {"principal": "p", "host": "h", "harness": "x",
                "expires_at": 1}}))
    dup = {"t1": {"principal": "dup", "host": "h", "harness": "x"},
           "t2": {"principal": "dup", "host": "h", "harness": "x"}}
    assert any("duplicate principal id" in p for p in problems_for(dup))
    assert problems_for({"": {"principal": "p", "host": "h",
                              "harness": "x"}}), "empty token key refused"
    over_ns = {"t1": {"principal": "p", "host": "h", "harness": "x",
                      "namespaces": ["n%d" % i for i in range(129)]}}
    assert any("maximum" in p for p in problems_for(over_ns))
    long_ns = {"t1": {"principal": "p", "host": "h", "harness": "x",
                      "namespaces": ["x" * 257]}}
    assert any("UTF-8 bytes" in p for p in problems_for(long_ns))
    many = {"tok-%032d" % i: {"principal": "p%d" % i, "host": "h",
                              "harness": "x"} for i in range(257)}
    assert any("maximum of 256" in p for p in problems_for(many))
    # Grammar corpus (review round 1): rules the hub parser enforces.
    assert any("principal" in p and "must match" in p for p in problems_for(
        {"t1": {"principal": "bad id!", "host": "h", "harness": "x"}}))
    assert any("'host' must match" in p for p in problems_for(
        {"t1": {"principal": "p", "host": "bad host!", "harness": "x"}}))
    assert any("'harness' must match" in p for p in problems_for(
        {"t1": {"principal": "p", "host": "h", "harness": ""}}))
    assert any("capabilities" in p for p in problems_for(
        {"t1": {"principal": "p", "host": "h", "harness": "x",
                "capabilities": ["root"]}}))
    assert any("capabilities" in p for p in problems_for(
        {"t1": {"principal": "p", "host": "h", "harness": "x",
                "capabilities": []}}))
    assert any("token exceeds" in p for p in problems_for(
        {"t" * 5000: {"principal": "p", "host": "h", "harness": "x"}}))
    assert any("non-empty" in p for p in problems_for(
        {"t1": {"principal": "p", "host": "h", "harness": "x",
                "namespaces": ["  "]}}))


def test_issuer_and_doctor_match_server_auth_grammar():
    """Tri-party parity: the issuer's and doctor's grammar constants equal
    the server parser's (review round 1 — a map the hub exits-2 on must
    never be minted or reported valid)."""
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-tri-") as td:
        env = T12._base_env(Path(td) / "home")
        code = (
            "import sys\n"
            "sys.path.insert(0, {scripts!r})\n"
            "sys.path.insert(0, {auth!r})\n"
            "import doctor\n"
            "try:\n"
            "    import auth as server_auth\n"
            "except Exception as exc:\n"
            "    print('AUTH-IMPORT-FAILED', type(exc).__name__)\n"
            "    raise SystemExit(0)\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'issue_principal', r'{ip}')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "print('IDRE', doctor._FLEET_PRINCIPAL_ID_RE.pattern == "
            "server_auth._PRINCIPAL_ID_RE.pattern == "
            "mod._PRINCIPAL_ID_RE.pattern)\n"
            "print('CAPS', doctor._FLEET_ALLOWED_CAPABILITIES == "
            "server_auth._ALLOWED_CAPABILITIES == "
            "mod._ALLOWED_CAPABILITIES)\n"
            "print('LIM', doctor._FLEET_MAX_PRINCIPAL_ID_CHARS == "
            "server_auth._MAX_PRINCIPAL_ID_CHARS == "
            "mod._MAX_PRINCIPAL_ID_CHARS, "
            "doctor._FLEET_MAX_TAG_VALUE_CHARS == "
            "server_auth._MAX_PRINCIPAL_TAG_VALUE_CHARS == "
            "mod._MAX_TAG_VALUE_CHARS, "
            "doctor._FLEET_MAX_TOKEN_UTF8_BYTES == "
            "server_auth._MAX_BEARER_TOKEN_UTF8_BYTES)\n"
            "def auth_accepts(entries):\n"
            "    try:\n"
            "        server_auth._parse_principal_map(\n"
            "            __import__('json').dumps(entries), 'probe')\n"
            "        return True\n"
            "    except SystemExit:\n"
            "        return False\n"
            "maps = [\n"
            "    {{'tok-aaaaaaaaaaaaaaaaaaaaaaaaaaaa1': {{\n"
            "        'principal': 'ok-id', 'host': 'box-1', \n"
            "        'harness': 'claude-code'}}}},\n"
            "    {{'tok-aaaaaaaaaaaaaaaaaaaaaaaaaaaa2': {{\n"
            "        'principal': 'bad id!', 'host': 'h', \n"
            "        'harness': 'x'}}}},\n"
            "    {{'tok-aaaaaaaaaaaaaaaaaaaaaaaaaaaa3': {{\n"
            "        'principal': 'p', 'host': 'h', 'harness': 'x',\n"
            "        'capabilities': ['root']}}}},\n"
            "]\n"
            "for m in maps:\n"
            "    hub_ok = auth_accepts(m)\n"
            "    doc_problems = bool(doctor._fleet_principal_map_problems(\n"
            "        __import__('json').dumps(m))[0])\n"
            "    entry = list(m.values())[0]\n"
            "    issuer_problems = bool(mod._validate_entry(entry))\n"
            "    print('AGREE', hub_ok, not doc_problems, not issuer_problems)\n"
        ).format(scripts=str(SCRIPTS_DIR), auth=str(HERMES_SERVER_DIR),
                 ip=str(ISSUE_PRINCIPAL_PY))
        result = _py(code, env, timeout=120)
        out = result.stdout
        if "AUTH-IMPORT-FAILED" in out:
            import pytest
            pytest.skip(f"server auth import unavailable (mcp dep?): {out}")
        assert "IDRE True" in out, out
        assert "CAPS True" in out, out
        assert "LIM True True True" in out, out
        agrees = [ln for ln in out.splitlines() if ln.startswith("AGREE")]
        assert len(agrees) == 3, out
        for ln in agrees:
            # hub accepts  ==  doctor reports no problems  ==  issuer accepts
            parts = ln.split()
            assert parts[1] == parts[2] == parts[3], ln


def test_cleanup_training_staging_through_build_report():
    """The CLI cleanup path drives _cleanup_training_staging with
    TRAINING_OUTPUT (not repo_root) — pins the review-round-1 argument
    regression: the flag must clean the --training-output parent, never the
    repo root's parent."""
    # SCRIPTS_DIR stays on sys.path for the whole test: doctor's LAZY
    # storelib imports resolve at build_report call time, not import time.
    sys.path.insert(0, str(SCRIPTS_DIR))
    import doctor
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-clean-") as td:
        tmp = Path(td)
        # Disjoint parents so the correct call and the regressed call
        # (training_output swapped for repo_root) clean DIFFERENT trees:
        # staging siblings live beside the training-output FILE.
        out_parent = tmp / "a"
        repo_parent = tmp / "b"
        project = tmp / "project"
        out_parent.mkdir()
        repo_parent.mkdir()
        project.mkdir()
        repo_root = repo_parent / "repo"
        repo_root.mkdir()
        stale_target = out_parent / ".training-staging-old"
        stale_target.mkdir()
        old_ts = time.time() - 7200
        os.utime(stale_target, (old_ts, old_ts))
        decoy = repo_parent / ".training-staging-decoy"
        decoy.mkdir()
        os.utime(decoy, (old_ts, old_ts))
        report = doctor.build_report(
            project, repo_root,
            training_output=str(out_parent / "export-out"),
            cleanup_training_staging=True,
            training_staging_max_age_hours=0.001,
            confirm_no_training_export=True,
        )
        cleanup = report.get("training_staging_cleanup") or {}
        removed = cleanup.get("removed") or []
        assert any("training-staging-old" in str(r) for r in removed), cleanup
        assert not any("decoy" in str(r) for r in removed), cleanup
        assert decoy.exists(), "the repo-root sibling must never be touched"
        assert not stale_target.exists(), "the stale target was removed"


# ---------------------------------------------------------------------------
# (i) issue-principal behavior
# ---------------------------------------------------------------------------


def _run_issue(map_path, *extra, stdin_text=""):
    env = T12._base_env(map_path.parent / "home")
    return subprocess.run(
        [PYTHON, "-X", "utf8", str(ISSUE_PRINCIPAL_PY),
         "--map", str(map_path), *extra],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        input=stdin_text, timeout=90)


def test_issue_principal_success_and_map_shape():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-ip-") as td:
        tmp = Path(td)
        map_path = tmp / "map.json"
        result = _run_issue(
            map_path, "--principal", "broker-1", "--host", "vsan-1",
            "--harness", "claude-code", "--namespaces", "project:x")
        assert result.returncode == 0, result.stderr
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, (
            f"the token prints EXACTLY once: stdout={result.stdout!r}")
        token = lines[0]
        assert len(token) > 30, "token must be a real secret, not a stub"
        data = json.loads(map_path.read_text(encoding="utf-8"))
        assert token in data
        assert data[token]["principal"] == "broker-1"
        assert data[token]["namespaces"] == ["project:x"]


def test_issue_principal_duplicate_and_off_tailnet_preset():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-ip2-") as td:
        tmp = Path(td)
        map_path = tmp / "map.json"
        first = _run_issue(
            map_path, "--principal", "b1", "--host", "h", "--harness", "x")
        assert first.returncode == 0, first.stderr
        dup = _run_issue(
            map_path, "--principal", "b1", "--host", "h", "--harness", "x")
        assert dup.returncode == 2, (
            f"duplicate principal id must be refused: {dup.stdout!r}")
        assert not any(ln.strip() for ln in dup.stdout.splitlines() if
                       ln.strip()), "no token on refusal"
        preset = _run_issue(
            map_path, "--principal", "runner-1", "--host", "cloud",
            "--harness", "gha", "--preset", "off-tailnet")
        assert preset.returncode == 0, preset.stderr
        data = json.loads(map_path.read_text(encoding="utf-8"))
        caps = [v["capabilities"] for v in data.values()
                if v.get("principal") == "runner-1"]
        assert caps == [["read", "passive"]], caps


def test_issue_principal_contention_exits_3_without_output():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-lock-") as td:
        tmp = Path(td)
        map_path = tmp / "map.json"
        lock = map_path.with_name(map_path.name + ".issue.lock")
        lock.write_text("999999:held-by-test", encoding="utf-8")
        result = _run_issue(
            map_path, "--principal", "b", "--host", "h", "--harness", "x")
        assert result.returncode == 3, (
            f"a live lock must refuse with exit 3: rc={result.returncode} "
            f"stdout={result.stdout!r}")
        assert not result.stdout.strip(), "no token under contention"


def test_issue_principal_degraded_lock_refuses():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-deg-") as td:
        tmp = Path(td)
        map_path = tmp / "map.json"
        env = T12._base_env(tmp / "home")
        code = (
            "import sys\n"
            "sys.path.insert(0, r'{scripts}')\n"
            "import host\n"
            "real = host.acquire_lock\n"
            "host.acquire_lock = lambda path, stale: host._NO_LOCK_TOKEN\n"
            "sys.argv = ['issue-principal.py', '--map', r'{mp}', "
            "'--principal', 'b', '--host', 'h', '--harness', 'x']\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location("
            "'issue_principal', r'{ip}')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "print('RC', mod.main())\n"
        ).format(scripts=str(SCRIPTS_DIR), mp=str(map_path),
                 ip=str(ISSUE_PRINCIPAL_PY))
        result = _py(code, env)
        assert "RC 3" in result.stdout, (
            f"a degraded lock must refuse unlocked issuance: "
            f"{result.stdout!r} {result.stderr!r}")
        assert not map_path.exists(), "nothing written on degraded lock"


def test_issue_principal_concurrent_distinct_ids():
    with tempfile.TemporaryDirectory(prefix="zmem-t12s-conc-") as td:
        tmp = Path(td)
        map_path = tmp / "map.json"
        env = T12._base_env(tmp / "home")
        cmd = lambda pid: (  # noqa: E731
            [PYTHON, "-X", "utf8", str(ISSUE_PRINCIPAL_PY),
             "--map", str(map_path), "--principal", pid,
             "--host", "h", "--harness", "x"])
        procs = [subprocess.Popen(cmd(p), cwd=str(REPO_ROOT), env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for p in ("racer-a", "racer-b")]
        outs = [p.communicate(timeout=90) for p in procs]
        rcs = [p.returncode for p in procs]
        # Contract: every exit-0 process printed exactly one token; every
        # non-zero process printed none; every exit-0 principal survived.
        assert any(rc == 0 for rc in rcs), rcs
        data = json.loads(map_path.read_text(encoding="utf-8"))
        for rc, (out, err) in zip(rcs, outs):
            lines = [ln for ln in out.splitlines() if ln.strip()]
            if rc == 0:
                assert len(lines) == 1, (rc, out)
                assert lines[0] in data, "a printed token must be usable"
            else:
                assert not lines, (rc, out)
                assert rc == 3, (rc, err)
        survivors = {v.get("principal") for v in data.values()}
        for rc, pid in zip(rcs, ("racer-a", "racer-b")):
            if rc == 0:
                assert pid in survivors


# ---------------------------------------------------------------------------
# (j) installer statics
# ---------------------------------------------------------------------------


def test_installer_statics():
    fleet_scripts = sorted(
        p for p in (REPO_ROOT / "scripts" / "fleet").glob("*")
        if p.is_file())
    names = {p.name for p in fleet_scripts}
    assert {"install-hub.ps1", "install-hub.sh", "install-broker.ps1",
            "install-broker.sh", "issue-principal.py"} <= names, names
    for path in fleet_scripts:
        text = path.read_text(encoding="utf-8", errors="replace")
        # No bind site may pass a wildcard host (comments may name the
        # refusal, but no flag/value shape may).
        for bad in ('--host 0.0.0.0', '--host=0.0.0.0', '"0.0.0.0"',
                    "'0.0.0.0'"):
            assert bad not in text, (path.name, bad)
    for hub in ("install-hub.ps1", "install-hub.sh"):
        text = (REPO_ROOT / "scripts" / "fleet" / hub).read_text(
            encoding="utf-8")
        assert "--host 127.0.0.1" in text, (
            f"{hub} must pin the hub to 127.0.0.1")
    for sh in ("install-hub.sh", "install-broker.sh"):
        # The syntax check needs a WORKING bash — on some Windows boxes the
        # PATH `bash` is a broken WSL relay (the CI ubuntu leg always runs
        # this; a skip here is honest, not a hole).
        probe = subprocess.run(
            ["bash", "-c", "echo ok"], capture_output=True, text=True,
            timeout=30)
        if probe.returncode != 0:
            import pytest
            pytest.skip(f"no working bash on PATH (WSL relay broken): "
                        f"{probe.stderr[:120]!r}")
        result = subprocess.run(
            ["bash", "-n", str(REPO_ROOT / "scripts" / "fleet" / sh)],
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (sh, result.stderr)
