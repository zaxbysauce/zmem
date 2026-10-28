"""Issue #288 supplementary (UNFROZEN) pins — the t-series _supp companion.

The frozen checks (tests/test_t09_op_queue.py) pin the issue's ACs. This
file pins the guardrails the approved plan's Phase 4.2 section names:

- S1: the PK-before-mutation ordering — an applied op_id writes NOTHING on
  replay (killed by moving the SELECT after the INSERT).
- S2: apply_ops guard presence — the MCP handler literally calls
  ``_guard_namespace`` with the resolved caller (the content-classified
  scanner family; the frozen C7 scanner covers the four legacy tools).
- S3: queue bounds + seq discipline — the loud overflow refusal and the
  monotonic client_seq contract.

Runs standalone (CI runs ``python tests/test_t09_op_queue_supp.py``
directly).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


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
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL", "ZMEM_REMOTE_URL",
    "ZMEM_OPQUEUE_MAX_BYTES", "ZMEM_OPQUEUE_MAX_AGE_DAYS",
    "ZMEM_MCP_TOKENS_FILE",
)



def _tokens_file() -> str:
    # A real temp dir, NOT ZMEM_DATA-relative: this runs BEFORE _pinned sets
    # the scratch env, so a ZMEM_DATA-relative path would write into the
    # repo root.
    import tempfile as _tf
    path = os.path.join(_tf.mkdtemp(prefix="zmem-t09-supp-tokens-"), "tokens.json")
    Path(path).write_text(json.dumps({
        "t09-nomut": {"principal": "p-nomut", "host": "h",
                      "harness": "x",
                      "capabilities": ["read", "passive", "write"],
                      "namespaces": ["user:global"]},
        "t09-mut": {"principal": "p-mut", "host": "h",
                    "harness": "x", "capabilities": ["mutate"],
                    "namespaces": ["user:global"]},
    }), encoding="utf-8")
    _map_owner_only(path)
    return path


def _pinned(prefix: str, tokens_file: str | None = None):
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    tmp = tempfile.mkdtemp(prefix=f"zmem-t09-supp-{prefix}-{uuid.uuid4().hex}-")
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": "t09-supp-token",
    })
    if tokens_file is not None:
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ["ZMEM_MCP_TOKENS_FILE"] = tokens_file
    for mod in [k for k in list(sys.modules)
                if k == "fleet" or k.startswith("fleet.")
                or k == "mcp_server" or k == "host" or k == "redaction"
                or k.startswith("storelib.")]:
        del sys.modules[mod]
    return saved, tmp


def _restore(saved):
    for mod in [k for k in list(sys.modules)
                if k == "fleet" or k.startswith("fleet.")
                or k == "mcp_server" or k == "host" or k == "redaction"
                or k.startswith("storelib.")]:
        del sys.modules[mod]
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_s1_replay_writes_nothing():
    """S1: an applied op_id's replay must not write ANY row — not even
    applied_ops telemetry (the frozen C2 pins bytes; this pins the ORDERING
    that makes it true: the PK SELECT precedes every mutation)."""
    import subprocess
    import json
    store = os.environ["ZMEM_STORE"]
    ops_path = os.path.join(os.environ["ZMEM_DATA"], "s1.jsonl")
    op = {"op_id": uuid.uuid4().hex, "op": "add",
          "args": {"type": "fact", "content": "s1 replay probe row",
                   "namespace": "user:global"},
          "depends_on": None, "client_seq": 1, "principal": ""}
    Path(ops_path).write_text(json.dumps(op) + "\n", encoding="utf-8")
    argv = [sys.executable, str(REPO_ROOT / "skills/memory/scripts/store.py"),
            "apply-ops", "--in", ops_path, "--json"]
    r1 = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert r1.returncode == 0, r1.stderr[-200:]
    receipts1 = json.loads(r1.stdout)["receipts"]
    assert receipts1[0]["status"] == "committed", receipts1
    before = Path(store).read_bytes()
    r2 = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    receipts2 = json.loads(r2.stdout)["receipts"]
    assert receipts2[0]["status"] == "duplicate", receipts2
    after = Path(store).read_bytes()
    assert before == after, "replay of an applied op must not touch the store file"


def test_s2_guard_presence_in_apply_ops_handler():
    """S2: the apply_ops MCP handler literally calls _guard_namespace with
    the resolved caller (source-contract pin; the frozen C7 scanner's
    content-classification family)."""
    text = (REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py").read_text(
        encoding="utf-8")
    import re
    segs = re.split(r"^    @mcp\.tool\(\)$", text, flags=re.M)
    apply_seg = None
    for seg in segs:
        if "async def apply_ops(" in seg[:400]:
            apply_seg = seg
            break
    assert apply_seg is not None, "apply_ops tool segment not found"
    assert "_guard_namespace(" in apply_seg, (
        "apply_ops handler must call _guard_namespace")
    assert "caller=resolved" in apply_seg, (
        "the guard call must pass the resolved principal as caller=")


def test_s3_queue_bounds_and_seq():
    """S3: loud overflow refusal + monotonic client_seq."""
    from fleet import opqueue
    os.environ["ZMEM_OPQUEUE_MAX_BYTES"] = "2048"
    # small content so a few entries fit, then the bound refuses loudly
    entry = None
    refusals = 0
    try:
        for i in range(40):
            entry = opqueue.enqueue(
                "s3", "add",
                {"type": "fact", "content": f"bound probe row {i} " + "x" * 60,
                 "namespace": "user:global"})
    except opqueue.QueueOverflowError:
        refusals += 1
    assert refusals == 1, "the size bound must refuse loudly, never silently drop"
    assert entry is not None
    try:
        opqueue.enqueue("s3", "add", {"content": "x"}, client_seq=1)
        raise AssertionError("non-monotonic client_seq must raise")
    except ValueError:
        pass


def test_s4_overlay_wiring_and_capability_rules():
    """S4 (M3+M5 mutant killers): the [PENDING] overlay is wired into the
    read-moment ENVELOPE (not just the standalone helper — the frozen C3
    pins the helper, so the wiring needs its own pin), and the capability
    truth table holds (revoked-first, None-deny, empty-deny, legacy-allow)."""
    from fleet import broker, opqueue
    from storelib import ops

    opqueue.enqueue("local", "update",
                    {"id_or_dep": "some-id", "content": "s4 pending state"})
    recorded = {}

    class _RecHttp:
        def prefetch(self, query, **kwargs):
            recorded.update(kwargs)
            from fleet import transport as _t
            return _t._empty_envelope()

    envelope = broker._handle_read(
        _RecHttp(), os.environ, "UserPromptSubmit",
        {"prompt": "s4", "session_id": "s4", "cwd": os.getcwd()}, 0.0)
    text = envelope.get("additionalContext", "")
    assert "[PENDING]" in text, (
        f"the read envelope must carry the pending overlay; got: {text[:120]!r}")
    assert "s4 pending state" in text

    rules = ops.LegacyPrincipal()
    revoked = ops.LegacyPrincipal()
    revoked.revoked = True
    empty = ops.LegacyPrincipal()
    empty.capabilities = frozenset()
    with_mutate = ops.LegacyPrincipal()
    with_mutate.capabilities = frozenset({"mutate"})
    with_admin = ops.LegacyPrincipal()
    with_admin.capabilities = frozenset({"read", "admin"})
    readonly = ops.LegacyPrincipal()
    readonly.capabilities = frozenset({"read"})
    assert ops.mutate_allowed(revoked) is False, "revoked denies FIRST"
    assert ops.mutate_allowed(None) is False, "unknown principal denies"
    assert ops.mutate_allowed(empty) is False, "empty capability set denies"
    assert ops.mutate_allowed(rules) is True, "legacy allows"
    assert ops.mutate_allowed(with_mutate) is True
    assert ops.mutate_allowed(with_admin) is True
    assert ops.mutate_allowed(readonly) is False



def test_s5_request_context_capability_routing():
    """S5 (final-critic FIX-NOW b): the authenticated REQUEST-context
    principal (no tool-level param) must reach the CLI capability gate —
    the no-mutate leg rejects mutations, the mutate leg commits (the F3
    bypass closed; the no-context fail-closed leg is pinned by
    repro/probe_f3b.py shape)."""
    import asyncio
    import json as _json
    import sys as _sys

    tokens_path = os.environ.get("ZMEM_MCP_TOKENS_FILE", "")
    assert tokens_path, "the pinned env must set a tokens file"
    sys.path.insert(0, str(REPO_ROOT / "hermes-plugin" / "server"))
    import mcp_server  # noqa: PLC0415

    server = mcp_server.build_server(host="127.0.0.1", port=0,
                                     use_tls=False)
    add_a = uuid.uuid4().hex

    def batch():
        # fresh op ids per leg: earlier legs commit their ids, so a reused
        # id would replay as duplicate and the supersede target would
        # already be tombstoned.
        add_id = uuid.uuid4().hex
        return [
            {"op_id": add_id, "op": "add",
             "args": {"type": "fact",
                      "content": "s5 routing row " + add_id[:8],
                      "namespace": "user:global"},
             "depends_on": None, "client_seq": 1, "principal": ""},
            {"op_id": uuid.uuid4().hex, "op": "supersede",
             "args": {"id_or_dep": add_id, "reason": "s5 routing"},
             "depends_on": add_id, "client_seq": 2, "principal": ""},
        ]

    result = asyncio.run(server._tool_manager.call_tool(
        "apply_ops", {"ops": batch()}, context=None))
    # Leg 1 (no-context): in-process call_tool carries NO auth context
    # (map mode -> deny-all): fail-closed, whole-batch refusal.
    assert isinstance(result, dict) and result.get("ok") is False, result
    assert result.get("error") == "capability_denied", result

    # Legs 2-3 (authenticated routing): set the request auth context the
    # middleware would set (the tests/test_t02_principal.py pattern) so
    # current_principal() resolves the TOKEN principal while the tool-level
    # principal param stays EMPTY — the exact F3 bypass path.
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    class _AccessToken:  # attribute bag, matching the SDK shape in use
        def __init__(self, client_id):
            self.token = "tok"
            self.client_id = client_id
            self.scopes = []

    def _authed(oid, batch_ops):
        saved = auth_context_var.set(AuthenticatedUser(_AccessToken(oid)))
        try:
            return asyncio.run(server._tool_manager.call_tool(
                "apply_ops", {"ops": batch_ops}, context=None))
        finally:
            auth_context_var.reset(saved)

    def _verdicts(res):
        if not isinstance(res, dict):
            raise AssertionError(f"expected dict, got {type(res)}")
        return {r["op"]: r["status"] for r in res.get("receipts", [])}

    # no-mutate principal: add commits, supersede rejected.
    v_nomut = _verdicts(_authed("p-nomut", batch()))
    assert v_nomut.get("add") == "committed", v_nomut
    assert v_nomut.get("supersede") == "rejected", v_nomut
    # mutate principal: both commit.
    v_mut = _verdicts(_authed("p-mut", batch()))
    assert v_mut.get("add") == "committed", v_mut
    assert v_mut.get("supersede") == "committed", v_mut


def test_s6_u288_framing_and_torn_tail():
    """S6 (review B6/R9a): the journal is ASCII-escaped on write and split
    on newlines only — a U+2028 in content cannot tear it; a torn tail is
    preserved across rewrites and never fused by the next append."""
    from fleet import opqueue
    env6 = dict(os.environ)
    env6["ZMEM_OPQUEUE_MAX_BYTES"] = "10485760"
    tear = chr(0x2028)
    sep = chr(0x85)
    escaped_tear = chr(92) + "u2028"
    opqueue.enqueue("s6", "capture",
                    {"content": "line one" + tear + "line two " + sep
                                + " tail"},
                    env=env6)
    rows = opqueue.pending("s6", env=env6)
    assert len(rows) == 1, rows
    assert "line two" in rows[0]["args"]["content"], rows[0]["args"]
    qpath = opqueue.queue_path("s6", env=env6)
    raw = qpath.read_text(encoding="utf-8")
    assert escaped_tear in raw, "content must be ASCII-escaped on disk"
    assert tear not in raw, "a RAW U+2028 must never touch the journal"
    # Torn tail: corrupt the last record mid-line, then append.
    with qpath.open("a", encoding="utf-8") as h:
        h.write('{"op_id": "torn", "op": "capt')
    opqueue.enqueue("s6", "capture", {"content": "after tear"},
                    env=env6)
    entries, torn = opqueue._read_file(qpath)
    assert len(entries) == 2, entries
    assert '"torn"' in torn, torn[:80]
    assert entries[-1]["args"]["content"] == "after tear", entries[-1]
    # A receipt rewrite PRESERVES the torn tail instead of deleting it.
    opqueue.mark_receipts("s6", [{"op_id": entries[0]["op_id"],
                                  "status": "committed"}],
                          env=env6)
    entries2, torn2 = opqueue._read_file(qpath)
    assert len(entries2) == 2 and '"torn"' in torn2, (entries2, torn2[:80])


def test_s7_chunked_flush_and_empty_principal():
    """S7 (review B1/R2): flush chunks to the hub's 256-op limit, sends the
    EMPTY principal (the authenticated request context is the identity —
    a synthetic 'local' id is unroutable at the hub), and propagates the
    caller's deadline."""
    from fleet import opqueue
    calls = []

    class _RecHttp:
        def apply_ops(self, ops, principal="", deadline_ms=None,
                      client_protocol=None):
            calls.append({"n": len(ops), "principal": principal,
                          "deadline_ms": deadline_ms})
            return {"ok": True,
                    "receipts": [{"op_id": o["op_id"], "op": o["op"],
                                  "status": "committed",
                                  "canonical_id": "x",
                                  "deduped": False} for o in ops]}

    env = dict(os.environ)
    env["ZMEM_OPQUEUE_MAX_BYTES"] = "10485760"
    env["ZMEM_OPQUEUE_MAX_AGE_DAYS"] = "7"
    for i in range(257):
        opqueue.enqueue("s7", "capture", {"content": "row %d" % i},
                        client_seq=i + 1, env=env)
    result = opqueue.flush("s7", _RecHttp(), env,
                           deadline_ms=1234)
    assert result is not None and result.get("ok") is True, result
    assert [c["n"] for c in calls] == [256, 1], calls
    for c in calls:
        assert c["principal"] == "", (
            "the flush must send the EMPTY principal (request context is "
            "the identity), got %r" % c["principal"])
        assert c["deadline_ms"] == 1234, c
    assert len(opqueue.pending("s7", env=dict(os.environ))) == 0


def test_s8_cross_principal_id_isolation():
    """S8 (review PRR-016): applied_ops is keyed (op_id, principal) — the
    same op_id from two principals commits BOTH rows and neither sees the
    other's canonical id; a same-principal replay is still a duplicate."""
    from storelib import connect, init_db
    from storelib import ops as opslib
    conn = connect()
    init_db(conn)
    shared = uuid.uuid4().hex
    # Distinct content per principal: the store's content dedup FOLDS
    # identical text across principals (a store-level rule), which would
    # mask the id-isolation assertion this check makes.
    op = {"op_id": shared, "op": "add",
          "args": {"content": "isolation probe p-a", "namespace": "user:global"}}
    op_b = {"op_id": shared, "op": "add",
            "args": {"content": "isolation probe p-b",
                     "namespace": "user:global"}}
    r1 = opslib.apply_ops(conn, [dict(op)], principal="p-a")
    r2 = opslib.apply_ops(conn, [dict(op_b)], principal="p-b")
    assert r1["receipts"][0]["status"] == "committed", r1
    assert r2["receipts"][0]["status"] == "committed", (
        "the same op_id under a DIFFERENT principal is a fresh op, not a "
        "duplicate leak, got: %r" % r2)
    assert (r1["receipts"][0]["canonical_id"]
            != r2["receipts"][0]["canonical_id"]), (r1, r2)
    r3 = opslib.apply_ops(conn, [dict(op)], principal="p-a")
    assert r3["receipts"][0]["status"] == "duplicate", r3


class _Scoped:
    revoked = False
    capabilities = frozenset({"mutate"})
    principal_id = "p-scoped"
    scoped = True
    namespaces = frozenset({"project:allowed"})


class _Legacy:
    revoked = False
    capabilities = None
    principal_id = "legacy"
    scoped = False
    namespaces = None


def test_s9_namespace_scope_enforced_engine_side():
    """S9 (review B3/B4): a scoped principal cannot add into a
    non-allow-listed namespace (including the user:global DEFAULT of an
    omitted namespace), cannot mutate a foreign-namespace row by id, and
    the client taint claim never survives the route."""
    from storelib import connect, init_db
    from storelib import ops as opslib
    from storelib import write as writelib
    conn = connect()
    init_db(conn)
    foreign_id = str(writelib.add_memory(
        conn, namespace="project:foreign", type_="fact",
        content="foreign row", capture_mode="manual"))
    scoped = _Scoped()
    lookup = lambda pid: scoped if pid == "p-scoped" else None  # noqa: E731
    r = opslib.apply_ops(
        conn, [{"op_id": uuid.uuid4().hex, "op": "add",
                "args": {"content": "x", "namespace": "project:foreign"}}],
        principal="p-scoped", principal_lookup=lookup,
        default_principal=None)
    assert r["receipts"][0]["error"] == "namespace_not_allowed", r
    r = opslib.apply_ops(
        conn, [{"op_id": uuid.uuid4().hex, "op": "add",
                "args": {"content": "no-ns"}}],
        principal="p-scoped", principal_lookup=lookup,
        default_principal=None)
    assert r["receipts"][0]["error"] == "namespace_not_allowed", r
    r = opslib.apply_ops(
        conn, [{"op_id": uuid.uuid4().hex, "op": "update",
                "args": {"id_or_dep": foreign_id,
                         "content": "PWNED by scoped principal"}}],
        principal="p-scoped", principal_lookup=lookup,
        default_principal=None)
    assert r["receipts"][0]["error"] == "namespace_not_allowed", r
    # The unscoped operator route still commits the foreign-row update.
    r = opslib.apply_ops(
        conn, [{"op_id": uuid.uuid4().hex, "op": "update",
                "args": {"id_or_dep": foreign_id,
                         "content": "operator update"}}],
        principal="", principal_lookup=None, default_principal=_Legacy())
    assert r["receipts"][0]["status"] == "committed", r
    # The taint clamp: a client-claimed trusted_internal never survives.
    r = opslib.apply_ops(
        conn, [{"op_id": uuid.uuid4().hex, "op": "add",
                "args": {"content": "taint probe",
                         "namespace": "user:global",
                         "taint": "trusted_internal"}}],
        principal="", principal_lookup=None, default_principal=_Legacy())
    assert r["receipts"][0]["status"] == "committed", r
    row = conn.execute(
        "SELECT taint FROM memory WHERE id=?",
        (r["receipts"][0]["canonical_id"],)).fetchone()
    assert row is not None and row["taint"] != "trusted_internal", row


def test_s10_poison_op_is_per_op_rejected():
    """S10 (review R6/ML3R-04): a malformed op VALUE (depends_on as a
    list, a non-string namespace) is a per-op rejected receipt — the rest
    of the batch still commits and no traceback escapes."""
    from storelib import connect, init_db
    from storelib import ops as opslib
    conn = connect()
    init_db(conn)
    good = {"op_id": uuid.uuid4().hex, "op": "add",
            "args": {"content": "good row", "namespace": "user:global"}}
    poison_dep = {"op_id": uuid.uuid4().hex, "op": "add",
                  "args": {"content": "x", "namespace": "user:global"},
                  "depends_on": ["zz"]}
    poison_ns = {"op_id": uuid.uuid4().hex, "op": "add",
                 "args": {"content": "y", "namespace": 5}}
    result = opslib.apply_ops(conn, [good, poison_dep, poison_ns],
                              principal="", principal_lookup=None,
                              default_principal=_Legacy())
    receipts = result["receipts"]
    assert receipts[0]["status"] == "committed", receipts
    assert receipts[1]["status"] == "rejected", receipts
    assert receipts[2]["status"] == "rejected", receipts
    try:
        opslib.parse_ops_line('{"op_id":"a","op":"add","args":{},'
                              '"depends_on":["z"]}')
        raise AssertionError("non-string depends_on must be refused")
    except ValueError:
        pass


def test_s11_quarantine_under_default_intent():
    """S11 (review R24): under the shipped auto DEFAULT (no flag, no env),
    a quarantineable refusal (absolute file: source_ref) records the
    quarantine row and exits 0 — the intent-resolved mode, not the raw
    flag, drives the quarantine branch."""
    import subprocess
    scratch = tempfile.mkdtemp(prefix="zmem-t09-s11-")
    env = dict(os.environ)
    env["ZMEM_STORE"] = os.path.join(scratch, "store.sqlite")
    env["ZMEM_DATA"] = scratch
    env.pop("ZMEM_CAPTURE_MODE", None)
    env.pop("ZMEM_REMOTE_URL", None)
    store_py = str(SCRIPTS_DIR / "store.py")
    run = subprocess.run(
        [sys.executable, store_py, "add", "--namespace", "user:global",
         "--type", "fact", "--content", "quarantine probe",
         "--source-ref", "file:///etc/hosts", "--json"],
        env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180)
    assert run.returncode == 0, (
        "an auto-mode quarantineable refusal must exit 0 with a quarantine "
        "record, rc=%d stderr=%s" % (run.returncode, run.stderr[:300]))
    assert "quarantin" in run.stdout.lower(), run.stdout[:300]
    quarantine_dir = Path(scratch) / "quarantine"
    rows = list(quarantine_dir.glob("*.jsonl")) if quarantine_dir.exists() \
        else []
    assert rows, "the quarantine JSONL must exist in the data dir"



def main() -> int:
    if not MCP_AVAILABLE:
        print("SKIP: mcp package absent — tests/test_t09_op_queue_supp.py "
              "requires the 'mcp' package")
        return 0
    saved, _tmp = _pinned("main", tokens_file=_tokens_file())
    failures = []
    try:
        for fn in (test_s1_replay_writes_nothing,
                   test_s2_guard_presence_in_apply_ops_handler,
                   test_s3_queue_bounds_and_seq,
                   test_s4_overlay_wiring_and_capability_rules,
                   test_s5_request_context_capability_routing,
                   test_s6_u288_framing_and_torn_tail,
                   test_s7_chunked_flush_and_empty_principal,
                   test_s8_cross_principal_id_isolation,
                   test_s9_namespace_scope_enforced_engine_side,
                   test_s10_poison_op_is_per_op_rejected,
                   test_s11_quarantine_under_default_intent):
            try:
                fn()
                print(f"PASS {fn.__name__}")
            except AssertionError as exc:
                failures.append((fn.__name__, str(exc)))
                print(f"FAIL {fn.__name__}: {exc}")
    finally:
        _restore(saved)
    if failures:
        print(f"{len(failures)} supp check(s) failed")
        return 1
    print("all supp checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
