"""Issue #285 (Workstream T PR 6) — authenticated hello handshake.

Phase 2.5 frozen acceptance checks C1-C3 (AC4's preserving check stays in
tests/test_mcp_server.py::HealthHelperCITest::test_compute_health_returns_
minimal_shape). Every check names its standing at both ends:

- AC1 ``test_hello_reports_protocol_and_profiles`` — the authenticated
  ``hello`` tool returns a dict carrying ``protocol``, ``event_schema``,
  ``capture_policy``, ``store_schema``, ``embed_profile``,
  ``reranker_profile``, ``zmem_version`` and ``capabilities``, every field
  populated; the identity fields agree with the single sources of truth
  (skills/memory/scripts/protocol.py, schema_meta.py, embed_profiles.py,
  cross_encoder_profiles.py, release-manifest.json). RED at base (no
  ``hello`` tool; no protocol constants module), GREEN after the fix.
- AC2 ``test_incompatible_client_write_is_refused`` — ``add`` carrying an
  unsupported ``client_protocol`` major is refused with the stable error
  token ``protocol_incompatible`` and stores NOTHING (zero rows in the
  isolated store — the refusal happens before any store.py subprocess).
  A control leg proves the harness can write: the same ``add`` WITHOUT
  ``client_protocol`` stores exactly one row. RED at base (the kwarg is
  silently ignored and the row is stored), GREEN after the fix.
- AC3 ``test_incompatible_client_read_fails_open_with_reason`` — a passive
  ``prefetch`` from an incompatible client returns the EMPTY envelope with
  ``reason == "protocol-incompatible"`` (fail open, distinguishable from
  ``empty-pool``; ``context``/``rendered`` empty). A control leg proves the
  same call without ``client_protocol`` never reports
  ``protocol-incompatible``. RED at base, GREEN after the fix.

Supplementary (same file, not frozen AC rows):
- ``test_session_start_passive_read_reason`` — the other passive surface
  (session_start) refuses identically (reason ``protocol-incompatible``,
  empty context), mirroring the ZMEM_INJECT=0 twin's shape.
- ``test_supported_writes_and_reads_keep_legacy_shape`` — absent
  ``client_protocol`` (legacy client, protocol 1) keeps today's behavior
  for one release: a write succeeds and a passive read returns the normal
  envelope (reason != protocol-incompatible).

Authored tool contract the implementation must satisfy (the issue's
constraints, made executable):
- ``client_protocol`` is OPTIONAL on every tool except ``hello``; absent
  means legacy protocol 1 (compatible for one release).
- An unsupported major refuses writes with
  ``{"error": "protocol_incompatible", ...}`` before any subprocess.
- ``hello`` takes no arguments and needs no store subprocess.

Env discipline: every test pins a fresh GUID scratch dir (ZMEM_HOME=repo
root, ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR, ZMEM_MODEL_AUTODOWNLOAD=0,
PYTHONIOENCODING=utf-8, a scratch ZMEM_MCP_TOKEN) BEFORE importing
mcp_server, purging any cached ``mcp_server`` module so constants reload
under the pinned env, and restores everything afterwards.

Runs standalone (CI runs ``python tests/test_t06_hello.py`` directly — the
repo's python lane is a direct-execution loop, NOT pytest): the __main__
block invokes every check, prints failures by name, and exits 1 on any
failure. Skips loudly (visible message, exit 0) only when the ``mcp``
package is absent, which CI prevents by installing
hermes-plugin/server/requirements.txt on both matrix legs.
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import asyncio
import contextlib
import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

try:
    import mcp  # noqa: F401  (the SDK import that gates the whole surface)
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs requirements.txt
    MCP_AVAILABLE = False

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT", "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_DEFAULT_NS",
    "ZMEM_EMBED_PROFILE", "ZMEM_HOST", "ZMEM_SESSION",
)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


@contextlib.contextmanager
def _pinned_server(prefix: str):
    """Fresh GUID scratch + pinned env + a real build_server() object.

    mcp_server is (re)imported under the pinned environment so its
    import-time constant loading follows THIS scratch; the module is purged
    from sys.modules on entry AND exit so sibling tests never inherit the
    pin (store-path-freeze discipline).
    """
    tmp = tempfile.mkdtemp(prefix=f"zmem-t06-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": "t06-check-token",
    })
    for mod in [k for k in list(sys.modules)
                if k == "mcp_server" or k.startswith("auth")
                or k.startswith("bind_guard")]:
        del sys.modules[mod]
    sys.path.insert(0, str(SERVER_DIR))
    try:
        import mcp_server
        server = mcp_server.build_server(
            host="127.0.0.1", port=0, use_tls=False)
        yield server, mcp_server
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
    """Invoke one tool through the real ToolManager (context-free)."""
    return asyncio.run(
        server._tool_manager.call_tool(name, args, context=None))


def _live_row_count(store_path: str) -> int:
    """Rows in the isolated store's memory table (0 when file absent)."""
    if not os.path.exists(store_path):
        return 0
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0
    finally:
        conn.close()


def _load_scripts_module(rel: str, attr: str):
    path = SCRIPTS_DIR / rel
    if not path.is_file():
        raise AssertionError(
            f"{rel} is missing at this tree (issue #285 fix not present)")
    spec = importlib.util.spec_from_file_location(
        f"t06_{path.stem}_{uuid.uuid4().hex[:6]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = getattr(module, attr, None)
    if value is None:
        raise AssertionError(f"{rel} does not expose {attr}")
    return value


def _protocol_constant(attr: str):
    return _load_scripts_module("protocol.py", attr)


# -- AC1 ----------------------------------------------------------------------

def test_hello_reports_protocol_and_profiles():
    with _pinned_server("c1") as (server, _mod):
        try:
            resp = _call(server, "hello", {})
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"hello tool not available: {exc}") from exc
        assert isinstance(resp, dict), f"hello must return a dict, got {resp!r}"
        expected_keys = (
            "protocol", "event_schema", "capture_policy", "store_schema",
            "embed_profile", "reranker_profile", "zmem_version",
            "capabilities",
        )
        for key in expected_keys:
            assert key in resp, f"hello response missing {key!r}: {resp!r}"
        # Identity fields agree with their single sources of truth.
        assert resp["protocol"] == _protocol_constant("PROTOCOL_MAJOR"), (
            f"protocol major must come from protocol.py: {resp['protocol']!r}")
        assert resp["event_schema"] == _protocol_constant(
            "EVENT_SCHEMA_VERSION"), resp["event_schema"]
        assert resp["capture_policy"] == _protocol_constant(
            "CAPTURE_POLICY_ID"), resp["capture_policy"]
        assert isinstance(resp["capture_policy"], str) \
            and resp["capture_policy"], "capture_policy must be populated"
        assert resp["store_schema"] == _load_scripts_module(
            "schema_meta.py", "SUPPORTED_SCHEMA_VERSION"), resp["store_schema"]
        assert resp["embed_profile"] == _load_scripts_module(
            "embed_profiles.py", "resolve_active_profile")(), \
            resp["embed_profile"]
        assert isinstance(resp["embed_profile"], str) \
            and resp["embed_profile"], "embed_profile must be populated"
        assert isinstance(resp["reranker_profile"], str) \
            and resp["reranker_profile"], (
                f"reranker_profile must be populated: "
                f"{resp['reranker_profile']!r}")
        # Plan-critic F-2: pin the reranker id to its source of truth, not
        # just "non-empty string" (a drifted fallback literal must fail).
        assert resp["reranker_profile"] == _load_scripts_module(
            "cross_encoder_profiles.py", "DEFAULT_PROFILE"), (
                f"reranker_profile must report the hub's cross-encoder "
                f"profile id: {resp['reranker_profile']!r}")
        assert isinstance(resp["zmem_version"], str) \
            and _SEMVER_RE.fullmatch(resp["zmem_version"]), (
                f"zmem_version must be a populated semver string: "
                f"{resp['zmem_version']!r}")
        caps = resp["capabilities"]
        assert isinstance(caps, list) and caps, (
            f"capabilities must be a non-empty list: {caps!r}")
        assert all(isinstance(c, str) and c for c in caps), caps
        assert caps == sorted(caps), "capabilities must be sorted/deterministic"
        assert "protocol-negotiation" in caps, caps


# -- AC2 ----------------------------------------------------------------------

def test_incompatible_client_write_is_refused():
    marker = f"t06-ac2-{uuid.uuid4().hex}"
    with _pinned_server("c2") as (server, _mod):
        store_path = os.environ["ZMEM_STORE"]
        refused = _call(server, "add", {
            "type": "fact",
            "content": f"{marker} refused",
            "namespace": "user:global",
            "client_protocol": 99,
        })
        assert isinstance(refused, dict), refused
        assert refused.get("error") == "protocol_incompatible", (
            f"unsupported-major write must refuse with the stable error "
            f"protocol_incompatible, got: {refused!r}")
        assert _live_row_count(store_path) == 0, (
            "a refused write must store nothing (no store.py subprocess "
            "may run for an incompatible client)")
        # Control leg: the same add WITHOUT client_protocol (legacy client)
        # must store exactly one row — proves the harness can write and the
        # refusal is the protocol gate, not a broken fixture.
        stored = _call(server, "add", {
            "type": "fact",
            "content": f"{marker} control",
            "namespace": "user:global",
        })
        assert isinstance(stored, dict) and not stored.get("error"), (
            f"control add (no client_protocol) must succeed: {stored!r}")
        assert _live_row_count(store_path) == 1, (
            "control add must have stored exactly one row")


# -- AC3 ----------------------------------------------------------------------

def test_incompatible_client_read_fails_open_with_reason():
    session = f"t06-ac3-{uuid.uuid4().hex}"
    with _pinned_server("c3") as (server, _mod):
        resp = _call(server, "prefetch", {
            "query": "t06 protocol check",
            "namespace": "user:global",
            "session_id": session,
            "moment": "user_prompt",
            "client_protocol": 99,
        })
        assert isinstance(resp, dict), resp
        assert resp.get("results") == [], (
            f"incompatible passive read must return the empty envelope, "
            f"got: {resp!r}")
        assert resp.get("reason") == "protocol-incompatible", (
            f"reason must be protocol-incompatible (distinguishable from "
            f"empty-pool), got: {resp.get('reason')!r}")
        assert (resp.get("context") or "") == "", resp
        assert (resp.get("rendered") or "") == "", resp
        # Control leg: the same prefetch WITHOUT client_protocol returns a
        # REAL selector envelope (results list + rendered fence present),
        # never the protocol refusal — legacy clients keep working.
        ok_resp = _call(server, "prefetch", {
            "query": "t06 protocol check",
            "namespace": "user:global",
            "session_id": session + "-ok",
            "moment": "user_prompt",
        })
        assert isinstance(ok_resp, dict), ok_resp
        assert isinstance(ok_resp.get("results"), list), (
            f"legacy prefetch must still return the results envelope: "
            f"{ok_resp!r}")
        assert "rendered" in ok_resp, ok_resp
        assert ok_resp.get("reason") != "protocol-incompatible", ok_resp


# -- supplementary (not frozen AC rows) ----------------------------------------

def test_session_start_passive_read_reason():
    session = f"t06-ss-{uuid.uuid4().hex}"
    with _pinned_server("ss") as (server, _mod):
        resp = _call(server, "session_start", {
            "session_id": session,
            "client_protocol": 99,
        })
        assert isinstance(resp, dict), resp
        assert resp.get("reason") == "protocol-incompatible", (
            f"session_start must mirror the passive refusal: {resp!r}")
        assert (resp.get("context") or "") == "", resp


def test_supported_writes_and_reads_keep_legacy_shape():
    marker = f"t06-legacy-{uuid.uuid4().hex}"
    with _pinned_server("legacy") as (server, _mod):
        store_path = os.environ["ZMEM_STORE"]
        stored = _call(server, "add", {
            "type": "fact",
            "content": marker,
            "namespace": "user:global",
            "client_protocol": 1,
        })
        assert isinstance(stored, dict) and not stored.get("error"), (
            f"protocol-1 (legacy/current) write must succeed: {stored!r}")
        assert _live_row_count(store_path) == 1
        resp = _call(server, "prefetch", {
            "query": "t06 legacy shape",
            "namespace": "user:global",
            "session_id": f"t06-legacy-{uuid.uuid4().hex}",
            "moment": "user_prompt",
            "client_protocol": 1,
        })
        assert isinstance(resp, dict), resp
        assert isinstance(resp.get("results"), list), resp
        assert "rendered" in resp, resp
        assert resp.get("reason") != "protocol-incompatible", resp


# -- standalone runner (CI direct-execution lane) -------------------------------

def _all_checks():
    return (
        test_hello_reports_protocol_and_profiles,
        test_incompatible_client_write_is_refused,
        test_incompatible_client_read_fails_open_with_reason,
        test_session_start_passive_read_reason,
        test_supported_writes_and_reads_keep_legacy_shape,
    )


def _main() -> int:
    if not MCP_AVAILABLE:
        print("SKIP: tests/test_t06_hello.py requires the 'mcp' package "
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
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
