"""Issue #283 (T-04) authorization-aware selection checks, Phase 2.5.

Turns the four acceptance criteria of the issue into executable checks
against the REAL surfaces (store.py subprocess legs and the FastMCP
ToolManager prefetch tool — no live server, no network):

- AC1 ``test_scoped_principal_keeps_authorized_project_rows`` — a
  principal scoped to ``project:alpha`` only (no ``user:global`` in its
  allow-list) prefetching ``project:alpha`` keeps the authorized project
  rows and omits ``user:global`` rows. RED at base, GREEN after the fix
  (base returns the empty ``global-not-allowed`` envelope).
- AC2 ``test_local_and_mcp_prefetch_envelopes_match_unscoped`` — the same
  fleet-fixture store (T-01 seed.jsonl replayed verbatim, then each row
  lifted over the prefetch confidence floor via the public append-only
  ``update`` verb because the seed recipe's default confidence 0.2 sits
  below the selector's CONFIDENCE_FLOOR 0.25 and could never surface),
  query (the recorded recall UserPromptSubmit prompt from moments.json),
  session 00000000-0000-4000-8000-000000000280 and moment, evaluated
  through the local CLI prefetch subcommand and the MCP prefetch tool
  with an UNSCOPED principal, return identical ``rendered`` and
  ``candidate_ids``. GREEN at base and head.
- AC3 ``test_scoped_ops_tokens_admit_only_allowed_namespaces`` — a scoped
  principal whose allow-list covers ``project:alpha`` AND ``project:beta``
  (beta is not the current project) passing hazard operation tokens at the
  ``pretool`` moment gets the allow-listed cross-project row admitted while
  a foreign ``project:gamma`` row is never admitted. RED at base, GREEN
  after the fix (base strips ops tokens wholesale for scoped principals
  and denies the whole call with the empty ``global-not-allowed``
  envelope).
- AC4 ``test_issue236_foreign_rows_never_admitted`` — issue #236 replay:
  scoped token (allow-list includes ``user:global`` so the store leg runs),
  foreign-project grounded rows, hazard ops tokens — zero foreign-project
  rows in results and rendered while the own-project row IS delivered.
  GREEN at base and head.

Env discipline: every test is a module-level function that pins a fresh
GUID scratch dir (ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR/
ZMEM_MODEL_AUTODOWNLOAD plus the ZMEM_TEST_NOW/PYTHONIOENCODING
determinism seams) BEFORE any storelib import or store.py subprocess,
strips every prefetch-affecting ambient override (kill switch, budget,
ledger window/cap, namespace, MCP token config, cross-project env), and
restores the saved environment afterwards. Each test seeds its own store
and uses unique session ids (the fleet-reserved id is reused only inside
AC2's two legs, with the delivery ledger reset between them, so the
ledger never suppresses rows as already-delivered).

Runs standalone: python tests/test_t04_authz_selection.py
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = str(REPO_ROOT / "skills" / "memory" / "scripts" / "store.py")
SERVER_PY = REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py"
FLEET_DIR = REPO_ROOT / "tests" / "fixtures" / "fleet"

# Determinism seam (same pin the prefetch parity suite uses).
PIN_TS = "2026-06-01T00:00:00Z"

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_TEST_NOW", "PYTHONIOENCODING",
    "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_DELIVER_WINDOW_S",
    "ZMEM_LEDGER_CAP", "ZMEM_NAMESPACE",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
    # The pretool legs (AC3/AC4) need the documented cross-tier defaults:
    # unset surface env (pretool-only) and the default hazard-verb set.
    "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
)


@contextlib.contextmanager
def _pinned_scratch(prefix: str):
    """Fresh GUID scratch + env pin (per-test; module-level functions)."""
    tmp = tempfile.mkdtemp(prefix=f"zmem-t04-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_TEST_NOW": PIN_TS,
        "PYTHONIOENCODING": "utf-8",
        # Bare env token = UNSCOPED operator token (test_mcp_auth
        # convention); scoped tests overwrite with ZMEM_MCP_TOKEN_FILE.
        "ZMEM_MCP_TOKEN": "t04-unscoped-operator",
    })
    for key in ("ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
                "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP", "ZMEM_NAMESPACE",
                "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
                "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS"):
        os.environ.pop(key, None)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_store(*args: str) -> subprocess.CompletedProcess:
    # issue #288 AC6 companion: CLI writes now default to the AUTO capture
    # intent whose redactor rewrites the 32-hex uuid tokens seeded INTO the
    # content — the rows then no longer match their own queries. The seeding
    # legs pin the MANUAL intent so content stays verbatim.
    env = dict(os.environ)
    env.setdefault("ZMEM_CAPTURE_MODE", "manual")
    return subprocess.run(
        [sys.executable, STORE_PY, *args],
        env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )


def _run_store_ok(*args: str) -> None:
    r = _run_store(*args)
    assert r.returncode == 0, f"store.py {args[0]} failed: {r.stderr}"


def _add_row(namespace: str, content: str) -> str:
    """Seed one grounded high-confidence row; returns its minted id."""
    r = _run_store("add", "--namespace", namespace, "--type", "fact",
                   "--content", content, "--signal", "test",
                   "--confidence", "0.9", "--json")
    assert r.returncode == 0, f"store.py add failed: {r.stderr}"
    return json.loads(r.stdout)["id"]


def _ledger_path(data_dir: str, session_id: str) -> Path:
    name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return Path(data_dir) / "ops" / (name + ".ledger")


def _reset_ledger(data_dir: str, session_id: str) -> None:
    """Delete the session's delivery ledger so the next leg re-delivers."""
    path = _ledger_path(data_dir, session_id)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


_MCP_MODULE = None


def _mcp_module():
    """Importlib-load hermes-plugin/server/mcp_server.py once (lazily, so
    the first load happens inside an already-pinned env)."""
    global _MCP_MODULE
    if _MCP_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "zmem_t04_mcp_server", SERVER_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["zmem_t04_mcp_server"] = mod
        spec.loader.exec_module(mod)
        _MCP_MODULE = mod
    return _MCP_MODULE


def _build_server():
    return _mcp_module().build_server(
        host="127.0.0.1", port=0, use_tls=False)


def _scoped_server(tmp: str, namespaces: list) -> object:
    """Build the server against a scoped file token (token resolved at
    build_server time, so env must be set BEFORE this call)."""
    token_file = os.path.join(tmp, "scoped-token.json")
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump({"token": f"t04-scoped-{uuid.uuid4().hex}",
                   "namespaces": namespaces}, f)
    os.environ["ZMEM_MCP_TOKEN_FILE"] = token_file
    os.environ.pop("ZMEM_MCP_TOKEN", None)
    return _build_server()


def _call_prefetch(server, **args) -> dict:
    return asyncio.run(server._tool_manager.call_tool(
        "prefetch", args, context=None))


def _result_ids(result: dict) -> set:
    return {row["id"] for row in result.get("results", [])
            if isinstance(row, dict) and row.get("id")}


def test_scoped_principal_keeps_authorized_project_rows():
    """AC1 (issue #283): a principal scoped to project:alpha only (no
    user:global in its allow-list) calls MCP prefetch for project:A on a
    store holding matching rows in project:A and user:global; the system
    shall return the project:A rows and omit the user:global rows (count >
    0, no global ids in results or rendered), not an empty
    global-not-allowed envelope. RED at base, GREEN after the fix."""
    if not MCP_AVAILABLE:
        raise unittest.SkipTest("mcp package not installed (MCP legs)")
    marker = f"t04-ac1-{uuid.uuid4().hex}"
    with _pinned_scratch("ac1") as tmp:
        _run_store_ok("init")
        alpha_id = _add_row(
            "project:alpha", f"{marker} authorized project row")
        global_id = _add_row(
            "user:global", f"{marker} global row that must stay omitted")
        server = _scoped_server(tmp, ["project:alpha"])
        result = _call_prefetch(
            server, query=marker, namespace="project:alpha",
            session_id=f"t04-ac1-{uuid.uuid4()}", moment="user_prompt")

    assert "error" not in result, result
    # The base tree answers the empty envelope; that is exactly the bug.
    assert result.get("reason") != "global-not-allowed", (
        "scoped principal denied its own authorized project rows: "
        f"{result}")
    assert result.get("count", 0) > 0, result
    ids = _result_ids(result)
    assert alpha_id in ids, (
        f"the authorized project:alpha row must be delivered: {result}")
    assert global_id not in ids, (
        f"user:global rows must be omitted for this principal: {result}")
    rendered = result.get("rendered") or ""
    assert "global row that must stay omitted" not in rendered, result
    assert "authorized project row" in rendered, result


def test_local_and_mcp_prefetch_envelopes_match_unscoped():
    """AC2 (issue #283): the same fleet-fixture store (T-01 seed.jsonl, 4
    store.py add rows in project:fixture-280), query (the recorded recall
    UserPromptSubmit prompt from moments.json), session
    00000000-0000-4000-8000-000000000280 and moment, evaluated through the
    local CLI prefetch subcommand (store.py subprocess) and through the
    MCP prefetch tool with an UNSCOPED principal, return identical
    ``rendered`` text and ``candidate_ids``. GREEN at base and head."""
    if not MCP_AVAILABLE:
        raise unittest.SkipTest("mcp package not installed (MCP legs)")
    seed_lines = [
        json.loads(line)
        for line in (FLEET_DIR / "seed.jsonl").read_text(
            encoding="utf-8").splitlines() if line.strip()]
    seed_lines.sort(key=lambda entry: entry["step"])
    recall = next(
        entry for entry in json.loads(
            (FLEET_DIR / "moments.json").read_text(encoding="utf-8")
        )["moments"] if entry["moment"] == "recall")
    payload = recall["payload"]
    query = payload["prompt"]
    session_id = payload["session_id"]
    namespace = payload["namespace"]
    assert len(query) < 1000, "prefetch query must stay under 1000 chars"
    assert session_id == "00000000-0000-4000-8000-000000000280", payload

    with _pinned_scratch("ac2") as tmp:
        seeded_ids = []
        for entry in seed_lines:
            argv = entry["argv"]
            r = _run_store(*argv)
            assert r.returncode == 0, (
                f"fleet seed step {entry['step']} failed: {r.stderr}")
            # "[zmem] added memory <uuid> (ns=..., ...)" — parse the id so
            # the confidence lift below targets exactly the seeded row.
            match = re.search(r"added memory ([0-9a-f-]{36})",
                              r.stdout + r.stderr)
            assert match, (
                f"fleet seed step {entry['step']} printed no row id: "
                f"{r.stdout} {r.stderr}")
            seeded_ids.append((match.group(1), argv))
        # DEVIATION (documented): the seed recipe ships store.py add's
        # default confidence 0.2, below the prefetch path's
        # CONFIDENCE_FLOOR 0.25 (storelib/schema.py) — replayed verbatim,
        # the rows can never surface through any selector call and the
        # parity claim would be vacuous. Replay stays byte-verbatim above;
        # lift each row over the floor through the public append-only
        # update verb (same content, confidence 0.9). The fixtures are
        # read-only and both parity legs share this ONE store, so parity
        # is still apples-to-apples modulo nothing.
        for row_id, argv in seeded_ids:
            content = argv[argv.index("--content") + 1]
            r = _run_store("update", "--id", row_id,
                           "--content", content, "--confidence", "0.9")
            assert r.returncode == 0, (
                f"confidence lift for {row_id} failed: {r.stderr}")

        # Leg 1 — local CLI prefetch subcommand (store.py subprocess).
        _reset_ledger(tmp, session_id)
        r = _run_store("prefetch", "--query", query,
                       "--namespace", namespace,
                       "--session-id", session_id,
                       "--moment", "user_prompt")
        assert r.returncode == 0, r.stderr
        cli = json.loads(r.stdout)

        # Leg 2 — MCP prefetch tool, unscoped operator token (the pin's
        # bare ZMEM_MCP_TOKEN), same store/query/session/moment; the
        # ledger is reset between legs so both legs deliver.
        server = _build_server()
        _reset_ledger(tmp, session_id)
        mcp = _call_prefetch(server, query=query, namespace=namespace,
                             session_id=session_id, moment="user_prompt")

    assert "error" not in mcp, mcp
    # Both legs must actually deliver, or the parity claim is vacuous.
    assert cli.get("count", 0) >= 1, cli
    assert mcp.get("count", 0) >= 1, mcp
    assert cli.get("rendered"), cli
    assert mcp.get("rendered"), mcp
    assert cli["rendered"] == mcp["rendered"], (
        "local CLI and MCP prefetch rendered fences diverged")
    assert cli.get("candidate_ids"), cli
    assert cli.get("candidate_ids") == mcp.get("candidate_ids"), (
        "local CLI and MCP prefetch candidate_ids diverged")


def test_scoped_ops_tokens_admit_only_allowed_namespaces():
    """AC3 (issue #283): a scoped principal whose allow-list includes
    project:alpha AND project:beta (beta is not the current project)
    passes hazard operation tokens at the pretool moment; cross-project
    candidates from project:beta (in the allow-list) shall be admitted
    while candidates from a foreign project:gamma shall NEVER be. RED at
    base, GREEN after the fix (base strips ops tokens wholesale for
    scoped principals and denies the call with the empty
    global-not-allowed envelope)."""
    if not MCP_AVAILABLE:
        raise unittest.SkipTest("mcp package not installed (MCP legs)")
    marker = f"t04-ac3-{uuid.uuid4().hex}"
    with _pinned_scratch("ac3") as tmp:
        _run_store_ok("init")
        _add_row("project:alpha", f"{marker} alpha current row")
        _add_row("project:beta", f"{marker} beta allowed cross row")
        _add_row("project:gamma", f"{marker} gamma foreign row")
        server = _scoped_server(tmp, ["project:alpha", "project:beta"])
        result = _call_prefetch(
            server, query=marker, namespace="project:alpha",
            session_id=f"t04-ac3-{uuid.uuid4()}", moment="pretool",
            lane="claude", ops_tokens=["stash", "pop"])

    assert "error" not in result, result
    assert result.get("reason") != "global-not-allowed", (
        "scoped principal denied its own authorized project rows: "
        f"{result}")
    assert result.get("count", 0) > 0, result
    ids = _result_ids(result)
    rendered = result.get("rendered") or ""
    # The allow-listed cross-project row from project:beta must be
    # admitted (store-side gates satisfied: pretool surface, hazard-verb
    # ops tokens "stash"/"pop", signal "test", live grounded row).
    assert "beta allowed cross row" in rendered, (
        "allow-listed cross-project rows must be admitted: "
        f"{result}")
    # The foreign project:gamma row must never be admitted.
    assert not any(
        row.get("namespace") == "project:gamma"
        for row in result.get("results", [])), result
    assert "gamma foreign row" not in rendered, (
        f"foreign-project rows leaked into the fence: {result}")


def test_issue236_foreign_rows_never_admitted():
    """AC4 (issue #283, replay of issue #236): a scoped token (allow-list
    includes user:global so the store leg actually runs), matching rows in
    the caller's project and a foreign project, and hazard operation
    tokens at the pretool moment — zero foreign-project rows appear in
    results and rendered while the own-project row IS delivered. GREEN at
    base and head."""
    if not MCP_AVAILABLE:
        raise unittest.SkipTest("mcp package not installed (MCP legs)")
    marker = f"t04-ac4-{uuid.uuid4().hex}"
    with _pinned_scratch("ac4") as tmp:
        _run_store_ok("init")
        own_id = _add_row(
            "project:allowed", f"{marker} own project row")
        _add_row("project:foreign", f"{marker} foreign secret row")
        server = _scoped_server(tmp, ["project:allowed", "user:global"])
        result = _call_prefetch(
            server, query=marker, namespace="project:allowed",
            session_id=f"t04-ac4-{uuid.uuid4()}", moment="pretool",
            lane="claude", ops_tokens=["stash", "pop"])

    assert "error" not in result, result
    # The own-project row must be delivered, or the no-leak claim below
    # would be vacuous.
    assert result.get("count", 0) >= 1, result
    ids = _result_ids(result)
    assert own_id in ids, f"own project row missing: {result}"
    rendered = result.get("rendered") or ""
    assert "own project row" in rendered, result
    # Zero foreign-project rows in results and rendered.
    assert not any(
        row.get("namespace", "").startswith("project:")
        and row.get("namespace") != "project:allowed"
        for row in result.get("results", [])), result
    assert "foreign secret row" not in rendered, (
        f"foreign-project rows leaked into the fence: {result}")


_TESTS = (
    test_scoped_principal_keeps_authorized_project_rows,
    test_local_and_mcp_prefetch_envelopes_match_unscoped,
    test_scoped_ops_tokens_admit_only_allowed_namespaces,
    test_issue236_foreign_rows_never_admitted,
)


if __name__ == "__main__":
    failures = []
    for test in _TESTS:
        name = test.__name__
        try:
            test()
        except unittest.SkipTest as exc:
            print(f"skip  {name}: {exc}")
            continue
        except AssertionError as exc:
            failures.append((name, f"AssertionError: {exc}"))
            print(f"FAIL  {name}: AssertionError: {exc}")
            continue
        except Exception as exc:  # a crashed check is a failed check
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            continue
        print(f"ok    {name}")
    if failures:
        print(f"\n{len(failures)} failing test(s):")
        for name, error in failures:
            print(f"  {name}: {error}")
        sys.exit(1)
    print("\nall tests passed")
    sys.exit(0)
