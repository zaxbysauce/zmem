"""Issue #285 feedback-round checks (swarm-pr-review pr387-20261020).

UNFROZEN companion to tests/test_t06_hello.py (which is checkpoint-frozen):
these pins cover the gaps the PR-387 review verified — the 10 tool gates
that had zero client_protocol coverage, the raw-value normalization
contract, the refusal payload shape, the deep-nesting crash fix (PRR-004),
hello's supported_protocol_majors field (PRR-023), the kill-switch
precedence on the passive surfaces (PRR-009), the session_start
decision-log leg (PRR-017), the principal-map capability branch (PRR-028),
the empty-window loader honor (PRR-007), and the miss_rate exclusion for
the new boundary reason (PRR-006).

Runs standalone (python tests/test_t06_hello_fb.py — the CI direct lane)
and under pytest. Skips loudly only when the mcp package is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
import subprocess


def _map_owner_only(path) -> None:
    """#406 contract: a test map must not depend on the directory's
    inherited ACL (non-system drives inherit Users/Authenticated-Users
    allow ACEs, which the hub correctly refuses). POSIX uses chmod."""
    if os.name == "nt":
        r = subprocess.run(
            ["icacls", os.fspath(path), "/inheritance:r",
             "/grant", os.environ.get("USERNAME", "") + ":(F)"],
            capture_output=True, timeout=30)
        assert r.returncode == 0, (
            f"icacls owner-only pin failed rc={r.returncode}: "
            f"{(r.stderr or r.stdout).strip()[:200]}")
    else:
        os.chmod(os.fspath(path), 0o600)


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs requirements.txt
    MCP_AVAILABLE = False

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE",
    "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_DEFAULT_NS", "ZMEM_EMBED_PROFILE",
)

_GATED_TOOLS = (
    "recall", "search", "recent", "evidence_for", "evidence_show",
    "add", "update", "supersede", "invalidate", "session_end",
    "prefetch_ack", "prefetch", "session_start",
)


@contextlib.contextmanager
def _pinned_server(prefix: str, *, inject: str | None = None,
                   tokens_file: str | None = None):
    tmp = tempfile.mkdtemp(prefix=f"zmem-t06fb-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    env = {
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": "t06fb-token",
    }
    if inject is not None:
        env["ZMEM_INJECT"] = inject
    if tokens_file is not None:
        env["ZMEM_MCP_TOKENS_FILE"] = tokens_file
    os.environ.update(env)
    for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
        del sys.modules[mod]
    sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_server
        server = mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        yield server, mcp_server, tmp
    finally:
        sys.path.remove(str(SERVER_DIR))
        for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
            del sys.modules[mod]
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _call(server, name: str, args: dict):
    return asyncio.run(
        server._tool_manager.call_tool(name, args, context=None))


def test_all_gated_tools_refuse_unsupported_major():
    """PRR-013: every one of the 13 gates refuses client_protocol=99 with
    the stable token — not just add/prefetch/session_start."""
    with _pinned_server("gates") as (server, _mod, _tmp):
        for name in _GATED_TOOLS:
            base_args: dict = {"client_protocol": 99}
            if name in ("recall", "search"):
                base_args["query"] = "fb gate probe"
            elif name == "recent":
                pass
            elif name in ("evidence_for",):
                base_args["memory_id"] = "fb-nonexistent"
            elif name == "evidence_show":
                base_args["id"] = "fb-nonexistent"
            elif name in ("update",):
                base_args.update({"id": "fb-nonexistent", "content": "x"})
            elif name in ("supersede",):
                base_args.update({"id": "fb-nonexistent"})
            elif name == "invalidate":
                base_args.update({"id": "fb-nonexistent", "reason": "fb"})
            elif name == "add":
                base_args.update({"type": "fact", "content": "fb"})
            elif name == "session_end":
                base_args["note"] = "fb"
            elif name == "prefetch_ack":
                base_args["reservation"] = "fb-nonexistent"
            elif name == "prefetch":
                base_args.update({
                    "query": "fb", "namespace": "user:global",
                    "session_id": "fb-" + uuid.uuid4().hex,
                    "moment": "user_prompt"})
            elif name == "session_start":
                base_args["session_id"] = "fb-" + uuid.uuid4().hex
            resp = _call(server, name, base_args)
            assert isinstance(resp, dict), (name, resp)
            if name in ("prefetch", "session_start"):
                # The two passive surfaces FAIL OPEN (#285 AC3): the empty
                # envelope with the boundary reason, never an error dict.
                assert resp.get("reason") == "protocol-incompatible", (
                    f"{name} must fail open with the boundary reason, "
                    f"got: {resp!r}")
            else:
                assert resp.get("error") == "protocol_incompatible", (
                    f"{name} must refuse with the stable token, got: {resp!r}")


def test_raw_value_normalization_at_the_gate():
    """PRR-013/F-1: bools, strings, floats, containers at client_protocol
    are refused (never coerced to protocol 1, never a transport crash)."""
    with _pinned_server("normalize") as (server, _mod, _tmp):
        for bad in (True, "1", 1.5, [1], {"major": 1}):
            resp = _call(server, "add", {
                "type": "fact", "content": "fb normalize",
                "namespace": "user:global", "client_protocol": bad})
            assert resp.get("error") == "protocol_incompatible", (
                f"client_protocol={bad!r} must refuse, got: {resp!r}")


def test_deeply_nested_client_protocol_still_refuses():
    """PRR-004: a deeply nested container must produce the stable refusal,
    not a RecursionError from the bounded render."""
    with _pinned_server("deep") as (server, _mod, _tmp):
        # Depth 990: above the in-path repr threshold (~956 at the tool
        # handler's stack depth, reviewer-measured; limit 1000), so the
        # probe enters the RecursionError branch the fix guards. Building
        # the list is iterative (no recursion); only repr recurses.
        deep: list = [99]
        for _ in range(990):
            deep = [deep]
        resp = _call(server, "add", {
            "type": "fact", "content": "fb deep",
            "namespace": "user:global", "client_protocol": deep})
        assert resp.get("error") == "protocol_incompatible", (
            f"deep nesting must still refuse with the stable token: "
            f"{str(resp)[:200]}")


def test_refusal_payload_shape():
    """PRR-015: the refusal carries the server identity and a bounded echo."""
    with _pinned_server("shape") as (server, _mod, _tmp):
        resp = _call(server, "add", {
            "type": "fact", "content": "fb shape",
            "namespace": "user:global", "client_protocol": 99})
        assert resp.get("server_protocol") == 1, resp
        assert resp.get("supported_protocol_majors") == [1], resp
        detail = resp.get("detail") or ""
        assert "hello" in detail, resp
        echo = resp.get("client_protocol") or ""
        assert isinstance(echo, str) and len(echo) <= 60, resp


def test_hello_reports_supported_window():
    """PRR-023/PRR-005: hello exposes the accepted window; the version is
    semver-resolved from the hub checkout."""
    import re
    with _pinned_server("hello-window") as (server, _mod, _tmp):
        resp = _call(server, "hello", {})
        assert resp.get("supported_protocol_majors") == [1], resp
        assert resp.get("protocol") == 1, resp
        version = resp.get("zmem_version")
        assert isinstance(version, str) and re.fullmatch(
            r"\d+\.\d+\.\d+", version or ""), (
                f"zmem_version must be semver in-checkout: {version!r}")


def test_kill_switch_precedes_protocol_gate():
    """PRR-009: with ZMEM_INJECT=0, a skewed client reads reason=disabled
    (the operator's boundary), never protocol-incompatible."""
    with _pinned_server("killswitch", inject="0") as (server, _mod, _tmp):
        resp = _call(server, "prefetch", {
            "query": "fb", "namespace": "user:global",
            "session_id": "fb-" + uuid.uuid4().hex,
            "moment": "user_prompt", "client_protocol": 99})
        assert resp.get("reason") == "disabled", (
            f"kill switch must dominate: {resp!r}")
        resp2 = _call(server, "session_start", {
            "session_id": "fb-" + uuid.uuid4().hex, "client_protocol": 99})
        assert resp2.get("reason") == "disabled", resp2


def test_session_start_refusal_writes_decision_line(tmp_expected_reason="protocol-incompatible"):
    """PRR-017: the session_start protocol refusal appends its decision-log
    line with the boundary reason (the kill-switch twin's parity)."""
    with _pinned_server("decision") as (server, _mod, tmp):
        session = "fb-" + uuid.uuid4().hex
        resp = _call(server, "session_start", {
            "session_id": session, "client_protocol": 99})
        assert resp.get("reason") == "protocol-incompatible", resp
        log = Path(tmp) / "zmem-decisions.log"
        assert log.is_file(), f"decision log must exist: {sorted(os.listdir(tmp))}"
        text = log.read_text(encoding="utf-8")
        assert "reason=protocol-incompatible" in text, text
        assert session not in text  # the caller string never reaches the log


def test_miss_rate_excludes_the_boundary_reason():
    """PRR-006: miss_rate's deliberate-silence exclusion covers
    protocol-incompatible exactly like disabled (the #133 class)."""
    src = (SCRIPTS_DIR / "storelib" / "miss_rate.py").read_text(encoding="utf-8")
    anchor = 'disabled_lines = [ln for ln in lines if ln.get("reason") in ('
    assert anchor in src, (
        "miss_rate must bucket deliberate-silence reasons via the shared "
        "exclusion tuple")
    idx = src.index(anchor)
    window = src[idx:idx + 300]
    assert '"protocol-incompatible"' in window, (
        "the exclusion tuple must name protocol-incompatible: "
        + window[:200])


def test_empty_window_tuple_is_honored():
    """PRR-007: SUPPORTED_PROTOCOL_MAJORS = () must stick (the legacy-close
    lever), not be skipped by a truthiness test."""
    tmp = tempfile.mkdtemp(prefix="zmem-t06fb-emptywin-")
    saved = {k: os.environ.get(k) for k in ("ZMEM_HOME", "ZMEM_STORE",
                                            "ZMEM_DATA", "ZMEM_MCP_TOKEN")}
    try:
        scripts = Path(tmp) / "skills" / "memory" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "protocol.py").write_text(
            "PROTOCOL_MAJOR = 1\n"
            "SUPPORTED_PROTOCOL_MAJORS = ()\n"
            "LEGACY_ABSENT_PROTOCOL_MAJOR = 1\n"
            "EVENT_SCHEMA_VERSION = 1\n"
            "CAPTURE_POLICY_ID = 'network-auto-redact-v1'\n"
            "ERROR_PROTOCOL_INCOMPATIBLE = 'protocol_incompatible'\n"
            "INJECT_REASON_PROTOCOL_INCOMPATIBLE = 'protocol-incompatible'\n"
            "def protocol_supported(major):\n    return False\n",
            encoding="utf-8")
        os.environ.update({
            "ZMEM_HOME": tmp,
            "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
            "ZMEM_DATA": tmp,
            "ZMEM_MCP_TOKEN": "t06fb-token",
        })
        for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
            del sys.modules[mod]
        sys.path.insert(0, str(SERVER_DIR))
        try:
            import mcp_server
            assert mcp_server._SUPPORTED_PROTOCOL_MAJORS == (), (
                f"empty window must be honored, got: "
                f"{mcp_server._SUPPORTED_PROTOCOL_MAJORS!r}")
        finally:
            sys.path.remove(str(SERVER_DIR))
            for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
                del sys.modules[mod]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_principal_map_capability_branch():
    """PRR-028: with a token map configured, hello reports the
    principal-map auth capability (the fleet-identity branch)."""
    tmp = tempfile.mkdtemp(prefix="zmem-t06fb-map-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    token = "fb-map-token-" + uuid.uuid4().hex
    tokens_path = Path(tmp) / "tokens.json"
    tokens_path.write_text(json.dumps({
        token: {"principal": "fbhost", "host": "fbhost",
                "harness": "fbharness", "namespaces": ["user:global"]},
    }), encoding="utf-8")
    _map_owner_only(tokens_path)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKENS_FILE": str(tokens_path),
    })
    for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
        del sys.modules[mod]
    sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_server
        server = mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        caps = _call(server, "hello", {}).get("capabilities") or []
        assert "auth:principal-map" in caps, caps
        assert "attribution-stamping" in caps, caps
    finally:
        sys.path.remove(str(SERVER_DIR))
        for mod in [k for k in list(sys.modules) if k == "mcp_server"]:
            del sys.modules[mod]
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _all_checks():
    return (
        test_all_gated_tools_refuse_unsupported_major,
        test_raw_value_normalization_at_the_gate,
        test_deeply_nested_client_protocol_still_refuses,
        test_refusal_payload_shape,
        test_hello_reports_supported_window,
        test_kill_switch_precedes_protocol_gate,
        test_session_start_refusal_writes_decision_line,
        test_miss_rate_excludes_the_boundary_reason,
        test_empty_window_tuple_is_honored,
        test_principal_map_capability_branch,
    )


def _main() -> int:
    if not MCP_AVAILABLE:
        print("SKIP: tests/test_t06_hello_fb.py requires the 'mcp' package "
              "(hermes-plugin/server/requirements.txt); not installed here.")
        return 0
    failures = []
    for check in _all_checks():
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures.append((check.__name__, str(exc)))
            print(f"FAIL {check.__name__}: {exc}")
        except SystemExit as exc:
            # A refusal path (e.g. an issue-#406 map-permission exit) must
            # surface as a named FAIL, not exit the driver silently after
            # earlier PASS lines.
            failures.append((check.__name__, f"SystemExit {exc.code}"))
            print(f"FAIL {check.__name__}: exited ({exc.code})")
        except Exception as exc:  # noqa: BLE001 - driver reports, never dies
            failures.append((check.__name__, repr(exc)))
            print(f"FAIL {check.__name__}: {exc!r}")
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all feedback checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
