"""Knowledge delivery tests (PR1, plan 2026-11-13).

The separate, capability-gated knowledge delivery lane — and its
isolation from the core operation queue:

  - CAPABILITY BEFORE QUEUE: hello must advertise ``knowledge_link_v1``
    before anything is enqueued; an old hub or an unreachable hub means
    explicit pending statuses and an EMPTY queue (never a core-queue
    fallback, never a protocol bump)
  - metadata only: the sidecar file never holds the note body, and no
    >=32-hex run exists anywhere in it (the digest travels as eight
    8-hex chunks)
  - bounded: the queue refuses at its bound instead of dropping; enqueue
    is idempotent per (target, key, locator)
  - flush against a CAPABLE hub backed by the REAL ops engine: receipts
    land, committed entries leave the queue, the journal reconciles
    pointer_queued -> applied, and a rejected entry leaves with its
    receipt (a resend of the same op_id would replay the verdict forever)
  - rollback pause: a hub that lost the capability (feature rollback)
    leaves every entry queued; nothing is sent
  - the transport method is SEPARATE: apply_knowledge_ops refuses a mixed
    core/knowledge batch client-side and carries only knowledge_link ops
  - engine admission: namespace scope FIRST, closed args whitelist
    (unknown keys / bad chunk digest / path-shaped values -> bad_op), and
    the capability split — write for first-create/noop, mutate for a
    replacement or ANY supplied expected_row_id
  - queue isolation: a knowledge capture never creates or touches the
    core op queue file, and the core queue never carries knowledge ops

The "hub" here is an in-process fake over the real engine/store (and a
stub call_fn for the transport method) — NOT a live hub; transport-level
claims are limited to the method contracts exercised this way.

Run: python tests/test_knowledge_delivery.py
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

_TMP = tempfile.mkdtemp(prefix="zmem-kndl-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")

import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

sys.path.insert(0, str(SCRIPTS_DIR))

from knowledge import workflow  # noqa: E402
from knowledge.config import load_knowledge_config  # noqa: E402
from knowledge.delivery import (  # noqa: E402
    CAPABILITY, KnowledgeDelivery, build_link_op, flush_to_hub,
    hub_capability_status)
from knowledge.documents import prepare_document  # noqa: E402
from knowledge.journal import KnowledgeJournal  # noqa: E402
from storelib import ops as opslib  # noqa: E402
from storelib.ops import LegacyPrincipal  # noqa: E402

NOTE = ("Fleet lesson: the hub's apply_ops receipts are the delivery "
        "record, never the enqueue.\n")
_HEX32 = re.compile(r"[0-9a-fA-F]{32,}")


def _init_store(tmp: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZMEM_")}
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [sys.executable, str(STORE_PY), "init"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        timeout=120)
    assert proc.returncode == 0, proc.stderr[-400:]
    return os.path.join(tmp, "store.sqlite")


def _open_rw(store_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(store_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class _Principal:
    """A minimal principal object for the engine's capability gates."""

    revoked = False
    principal_id = "test"
    namespaces = None
    scoped = False

    def __init__(self, capabilities):
        self.capabilities = capabilities


class _EngineHub:
    """A fake hub whose apply lane runs the REAL ops engine on a scratch
    store (hello capabilities and apply behavior are scriptable)."""

    def __init__(self, store_path: str, *, capabilities=(CAPABILITY,),
                 apply_mode="engine"):
        self.store_path = store_path
        self.capabilities = tuple(capabilities)
        self.apply_mode = apply_mode  # engine | fail | no-receipts
        self.ops_seen: list[list[dict]] = []
        self.hello_calls = 0

    def hello(self):
        self.hello_calls += 1
        return {"protocol": 1, "capabilities": list(self.capabilities),
                "zmem_version": "test-hub"}

    def apply_knowledge_ops(self, ops, principal: str = "", **_kw):
        self.ops_seen.append(copy.deepcopy(list(ops)))
        if self.apply_mode == "fail":
            return {"ok": False, "reason": "hub-unreachable"}
        if self.apply_mode == "no-receipts":
            return {"ok": True}
        conn = _open_rw(self.store_path)
        try:
            return opslib.apply_ops(
                conn, copy.deepcopy(list(ops)), principal=principal,
                default_principal=LegacyPrincipal())
        finally:
            conn.close()


class _Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-kndl-env-")
        self.vault = Path(self.tmp) / "vault"
        self.vault.mkdir()
        (Path(self.tmp) / "knowledge").mkdir()
        self.config_path = Path(self.tmp) / "config.json"
        self.config_path.write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                "notes": {"kind": "markdown", "root": str(self.vault)},
            }},
        }), encoding="utf-8")
        self.store_path = _init_store(self.tmp)
        self.journal = KnowledgeJournal(
            Path(self.tmp) / "knowledge" / "journal.json")
        self.delivery = KnowledgeDelivery(
            Path(self.tmp) / "knowledge" / "delivery.json")

    def config(self):
        return load_knowledge_config(self.config_path)

    def sidecar_text(self) -> str:
        return (Path(self.tmp) / "knowledge" / "delivery.json").read_text(
            encoding="utf-8")


def _link_op(op_id, *, version=1, text=None, expected=None,
             target="notes", key="run/a", namespace="user:global",
             extra=None):
    doc = prepare_document(target, key, text or ("op body %s\n" % version))
    args = {
        "namespace": namespace,
        "target": target,
        "key": key,
        "version": version,
        "locator": doc.locator,
        "fullhash": [doc.digest[i * 8:(i + 1) * 8] for i in range(8)],
        "summary": "s",
        "date": "2026-11-13T00:00:00Z",
        "expected_row_id": expected,
    }
    if extra:
        args.update(extra)
    return {"op_id": op_id, "op": "knowledge_link", "args": args}


def _digest_of(text: str) -> str:
    from knowledge.documents import canonical_content, content_digest

    return content_digest(canonical_content(text))


class CapabilityGateTests(unittest.TestCase):
    def test_capability_probe_outcomes(self):
        class _Hub:
            def hello(self):
                return {"protocol": 1,
                        "capabilities": ["apply-ops", CAPABILITY],
                        "zmem_version": "x"}
        self.assertEqual(hub_capability_status(_Hub())[0], "supported")
        class _Old:
            def hello(self):
                return {"protocol": 1, "capabilities": ["apply-ops"],
                        "zmem_version": "0.152.0"}
        status, detail = hub_capability_status(_Old())
        self.assertEqual(status, "unsupported")
        self.assertIn(CAPABILITY, detail)
        class _Dead:
            def hello(self):
                return {"ok": False, "reason": "hub-unreachable"}
        self.assertEqual(hub_capability_status(_Dead())[0], "unreachable")
        class _Raises:
            def hello(self):
                raise OSError("boom")
        self.assertEqual(hub_capability_status(_Raises())[0],
                         "unreachable")

    def test_old_hub_and_unreachable_never_enqueue(self):
        for hub in (
            _EngineHub("", capabilities=()),          # old hub
            _EngineHub("", apply_mode="fail"),        # unreachable apply
        ):
            with self.subTest(hub=hub.apply_mode or "old"):
                env = _Env()
                hub.store_path = env.store_path
                result = workflow.capture(
                    target_id="notes", logical_key="run/a", text=NOTE,
                    mode="remote", config=env.config(),
                    journal=env.journal, delivery=env.delivery, http=hub)
                self.assertEqual(result["pointer"]["status"],
                                 "pending" if not hub.capabilities
                                 else "queued")
                if not hub.capabilities:
                    self.assertEqual(env.delivery.pending(), [],
                                     "an old hub means NO enqueue")


class SidecarContractTests(unittest.TestCase):
    def test_metadata_only_and_chunked_digest(self):
        env = _Env()
        entry = env.delivery.enqueue({
            "namespace": "user:global", "target": "notes", "key": "run/a",
            "version": 1, "locator": "run/a/x.md",
            "fullhash": ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd",
                         "eeeeeeee", "ffffffff", "11111111", "22222222"],
            "summary": NOTE[:40], "date": "2026-11-13T00:00:00Z",
            "expected_row_id": None})
        text = env.sidecar_text()
        self.assertNotIn("never the enqueue", text,
                         "the note body must never reach the sidecar")
        self.assertIsNone(_HEX32.search(text),
                          "no >=32-hex run may exist in the sidecar")
        self.assertEqual(entry["status"], "queued")

    def test_enqueue_idempotent_and_bounded(self):
        env = _Env()
        base = {
            "namespace": "user:global", "target": "notes", "key": "run/a",
            "version": 1, "locator": "run/a/x.md",
            "fullhash": ["aaaaaaaa"] * 8, "summary": "s",
            "date": "2026-11-13T00:00:00Z", "expected_row_id": None}
        first = env.delivery.enqueue(dict(base))
        again = env.delivery.enqueue(dict(base))
        self.assertEqual(first["op_id"], again["op_id"])
        small = KnowledgeDelivery(
            Path(env.tmp) / "knowledge" / "small.json", max_queue=2)
        small.enqueue(dict(base, key="k1"))
        small.enqueue(dict(base, key="k2"))
        with self.assertRaises(Exception):
            small.enqueue(dict(base, key="k3"))
        self.assertEqual(len(small.pending()), 2)

    def test_queue_and_receipt_statuses_are_distinct(self):
        """queued != applied, explicitly, in the JSON surfaces."""
        env = _Env()
        hub = _EngineHub(env.store_path, apply_mode="fail")
        result = workflow.capture(
            target_id="notes", logical_key="run/a", text=NOTE,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(result["pointer"]["status"], "queued")
        self.assertTrue(result["pointer"]["enqueued"])
        self.assertEqual(result["pointer"]["flush"]["ok"], False)
        queued = env.delivery.pending()[0]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(env.delivery.receipts(), {})


class FlushTests(unittest.TestCase):
    def test_engine_backed_flush_applies_and_reconciles_journal(self):
        env = _Env()
        hub = _EngineHub(env.store_path, apply_mode="fail")
        result = workflow.capture(
            target_id="notes", logical_key="run/a", text=NOTE,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(result["pointer"]["status"], "queued")
        self.assertEqual(
            env.journal.entry_for("notes", "run/a", namespace="user:global").state,
            "pointer_queued")
        # The hub heals: flush drains the queue through the REAL engine.
        hub.apply_mode = "engine"
        flush = workflow.flush(journal=env.journal, delivery=env.delivery,
                               http=hub)
        self.assertTrue(flush["ok"], flush)
        self.assertEqual(flush["pending"], 0)
        self.assertEqual(len(flush["applied"]), 1)
        self.assertEqual(
            env.journal.entry_for("notes", "run/a", namespace="user:global").state, "applied")
        self.assertEqual(env.delivery.pending(), [])
        receipts = env.delivery.receipts()
        self.assertEqual(len(receipts), 1)
        receipt = next(iter(receipts.values()))
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual(receipt["link_status"], "created")
        # The pointer really landed on the engine's store.
        from storelib import knowledge as store_knowledge
        conn = _open_rw(env.store_path)
        try:
            pointer = store_knowledge.get_pointer(
                conn, "user:global", "notes", "run/a")
            self.assertIsNotNone(pointer)
            self.assertEqual(pointer.version, 1)
        finally:
            conn.close()

    def test_engine_replay_of_same_op_id_is_duplicate(self):
        env = _Env()
        hub = _EngineHub(env.store_path)
        op = _link_op("kl-replay")
        conn = _open_rw(env.store_path)
        try:
            first = opslib.apply_ops(conn, [op],
                                     default_principal=LegacyPrincipal())
            second = opslib.apply_ops(conn, [op],
                                      default_principal=LegacyPrincipal())
        finally:
            conn.close()
        self.assertEqual(first["receipts"][0]["status"], "committed")
        self.assertEqual(first["receipts"][0]["link_status"], "created")
        self.assertEqual(second["receipts"][0]["status"], "duplicate")
        self.assertNotIn("link_status", second["receipts"][0],
                         "the PK replay path replays the recorded verdict; "
                         "it never re-runs the handler")

    def test_rollback_pauses_pending(self):
        """A hub that LOST the capability (feature rollback) leaves every
        entry queued — the flush refuses to send, nothing is dropped."""
        env = _Env()
        hub = _EngineHub(env.store_path, apply_mode="fail")
        workflow.capture(
            target_id="notes", logical_key="run/a", text=NOTE,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        self.assertEqual(len(env.delivery.pending()), 1)
        # Rollback: the hub no longer advertises the capability.
        hub.capabilities = ()
        flush = flush_to_hub(env.delivery, hub)
        self.assertFalse(flush["ok"])
        self.assertEqual(flush["reason"], "hub-unsupported")
        self.assertTrue(flush["paused"])
        self.assertEqual(flush["pending"], 1)
        entry = env.delivery.pending()[0]
        self.assertGreaterEqual(entry["attempts"], 1)
        self.assertEqual(entry["last_error"], "hub-unsupported")
        # Capability restored (and the hub healthy again): the same entry
        # applies.
        hub.capabilities = (CAPABILITY,)
        hub.apply_mode = "engine"
        flush = flush_to_hub(env.delivery, hub)
        self.assertTrue(flush["ok"], flush)
        self.assertEqual(env.delivery.pending(), [])

    def test_rejected_entry_leaves_queue_with_receipt(self):
        """A rejected apply keeps its receipt and leaves the queue — the
        hub's applied_ops verdict on that op_id is final; a resend would
        replay the rejection forever."""
        env = _Env()
        # Seed the pointer at v1 directly through the engine, then send a
        # stale-CAS advance (wrong expected_row_id).
        conn = _open_rw(env.store_path)
        try:
            seeded = opslib.apply_ops(
                conn, [_link_op("kl-seed")],
                default_principal=LegacyPrincipal())
            self.assertEqual(seeded["receipts"][0]["status"], "committed")
        finally:
            conn.close()
        hub = _EngineHub(env.store_path)
        v2 = _link_op("kl-stale", version=2, text="stale stake body\n",
                      expected="not-the-live-row")
        entry = dict(v2["args"])
        entry["op_id"] = v2["op_id"]
        env.delivery.enqueue(entry)
        flush = flush_to_hub(env.delivery, hub)
        self.assertEqual(len(flush["rejected"]), 1)
        self.assertEqual(flush["rejected"][0]["error"],
                         "pointer_cas_stale")
        self.assertEqual(env.delivery.pending(), [])
        receipt = next(iter(env.delivery.receipts().values()))
        self.assertEqual(receipt["status"], "rejected")

    def test_flush_with_no_receipts_for_an_entry_keeps_it_queued(self):
        env = _Env()
        hub = _EngineHub(env.store_path, apply_mode="no-receipts")
        env.delivery.enqueue({
            "namespace": "user:global", "target": "notes", "key": "run/a",
            "version": 1, "locator": "run/a/x.md",
            "fullhash": ["aaaaaaaa"] * 8, "summary": "s",
            "date": "2026-11-13T00:00:00Z", "expected_row_id": None})
        flush = flush_to_hub(env.delivery, hub)
        self.assertFalse(flush["ok"])
        self.assertEqual(flush["reason"], "apply-failed")
        self.assertEqual(len(env.delivery.pending()), 1)


class _DenyingHub:
    """A capable hub that DENIES applies: whole-batch calls get
    ``batch_reason``; single-op calls are denied when the op's namespace is
    in ``denied_namespaces``, committed otherwise."""

    def __init__(self, *, batch_reason=None, denied_namespaces=()):
        self.batch_reason = batch_reason
        self.denied_namespaces = set(denied_namespaces)
        self.ops_seen: list[list[dict]] = []

    def hello(self):
        return {"protocol": 1, "capabilities": ["apply-ops", CAPABILITY],
                "zmem_version": "test-hub"}

    def apply_knowledge_ops(self, ops, principal: str = "", **_kw):
        import copy as _copy

        self.ops_seen.append(_copy.deepcopy(list(ops)))
        if len(ops) > 1 and self.batch_reason:
            return {"ok": False, "reason": self.batch_reason}
        denied = [op for op in ops
                  if op["args"]["namespace"] in self.denied_namespaces]
        if denied:
            return {"ok": False, "reason": "namespace_not_allowed"}
        return {"ok": True, "receipts": [
            {"op_id": op["op_id"], "status": "committed",
             "link_status": "created", "canonical_id": "row-x"}
            for op in ops]}


def _queue_entry(op_id, *, namespace="user:global", key="run/a",
                 version=1):
    args = _link_op(op_id, key=key, namespace=namespace,
                    version=version)["args"]
    return dict(args, op_id=op_id)


class WholeCallDenialTests(unittest.TestCase):
    """A whole-call hub DENIAL (scoped token / authorization) is terminal:
    the entry leaves the queue with a rejected receipt — never requeued
    forever as an apply-failed retry."""

    def test_single_entry_denial_is_terminal(self):
        env = _Env()
        hub = _DenyingHub(denied_namespaces={"user:global"})
        env.delivery.enqueue(_queue_entry("kl-deny"))
        result = flush_to_hub(env.delivery, hub)
        self.assertFalse(result["ok"])
        self.assertEqual(result["pending"], 0)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertEqual(result["rejected"][0]["op_id"], "kl-deny")
        self.assertEqual(result["rejected"][0]["error"],
                         "namespace_not_allowed")
        self.assertEqual(result["rejected"][0]["status"], "rejected")
        self.assertEqual(env.delivery.pending(), [])
        receipt = env.delivery.receipts()["kl-deny"]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["error"], "namespace_not_allowed")

    def test_mixed_batch_denial_splits_per_entry(self):
        env = _Env()
        hub = _DenyingHub(batch_reason="capability_denied",
                          denied_namespaces={"user:blocked"})
        env.delivery.enqueue(_queue_entry("kl-ok", key="run/ok"))
        env.delivery.enqueue(_queue_entry("kl-ns", key="run/ns",
                                          namespace="user:blocked"))
        result = flush_to_hub(env.delivery, hub)
        self.assertEqual(result["pending"], 0)
        # The authorized entry APPLIED; only the actually-denied one went
        # terminal — never indiscriminately.
        self.assertEqual(result["applied"], ["kl-ok"])
        self.assertEqual([r["op_id"] for r in result["rejected"]], ["kl-ns"])
        self.assertEqual(result["rejected"][0]["error"],
                         "namespace_not_allowed")
        self.assertEqual(env.delivery.pending(), [])
        # The batch denial was split into one call per entry.
        self.assertEqual([len(batch) for batch in hub.ops_seen], [2, 1, 1])


class TransportMethodTests(unittest.TestCase):
    """McpHttp.apply_knowledge_ops: the separate lane's client contract
    (exercised with a stub call_fn — NOT a live hub)."""

    @classmethod
    def setUpClass(cls):
        from fleet import transport
        cls.transport = transport

    def _http(self, recorded):
        async def call_fn(url, token, tool, args):
            recorded.append((tool, args))
            return {"ok": True, "receipts": []}

        return self.transport.McpHttp(
            url="http://127.0.0.1:9/mcp", executor=(
                self.transport.DeadlineExecutor()),
            call_fn=call_fn, deadline_s=5.0)

    def test_mixed_batch_refused_client_side(self):
        recorded: list = []
        http = self._http(recorded)
        result = http.apply_knowledge_ops([
            _link_op("kl-1"),
            {"op_id": "core-1", "op": "add",
             "args": {"content": "x", "namespace": "user:global"}},
        ])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "knowledge-batch-refused")
        self.assertEqual(recorded, [], "nothing may be sent for a mixed "
                         "batch")

    def test_knowledge_only_batch_reaches_apply_ops_tool(self):
        recorded: list = []
        http = self._http(recorded)
        ops = [_link_op("kl-1"), _link_op("kl-2", key="run/b")]
        result = http.apply_knowledge_ops(ops)
        self.assertEqual(result, {"ok": True, "receipts": []})
        self.assertEqual(len(recorded), 1)
        tool, args = recorded[0]
        self.assertEqual(tool, "apply_ops")
        self.assertEqual([o["op"] for o in args["ops"]],
                         ["knowledge_link", "knowledge_link"])

    def test_transport_failure_fails_closed(self):
        def call_fn(url, token, tool, args):
            async def _boom():
                raise OSError("connection refused")
            return _boom()

        http = self.transport.McpHttp(
            url="http://127.0.0.1:9/mcp",
            executor=self.transport.DeadlineExecutor(),
            call_fn=call_fn, deadline_s=5.0)
        result = http.apply_knowledge_ops([_link_op("kl-1")])
        self.assertFalse(result["ok"])
        self.assertIn(result["reason"],
                      ("hub-unreachable", "empty-pool"))

    class _Refused(RuntimeError):
        """mcp_client's ToolRefusalError shape (a RuntimeError whose
        safe_code is a whitelisted machine code)."""

        def __init__(self, code):
            super().__init__(code)
            self.safe_code = code

    def test_knowledge_refusal_code_exact_not_substring(self):
        classify = self.transport._knowledge_refusal_code
        self.assertEqual(
            classify(self._Refused("namespace_not_allowed")),
            "namespace_not_allowed")
        self.assertEqual(
            classify(self._Refused("capability_denied")),
            "capability_denied")
        # Generic error TEXT never classifies as a refusal (no substring
        # false positives — this is the empty-pool auth-bypass bug shape).
        self.assertIsNone(
            classify(RuntimeError("namespace access denied somewhere")))
        self.assertIsNone(classify(OSError("connection refused")))
        # The enum mirrors the server's actual whole-call denial codes.
        for code in ("namespace_not_allowed", "capability_denied",
                     "principal_mismatch"):
            self.assertIn(code, self.transport.KNOWLEDGE_REFUSAL_CODES)

    def test_knowledge_refusal_code_nested_http_401(self):
        import builtins

        try:
            import httpx
        except ImportError:
            self.skipTest("httpx not importable in this environment")
        if not hasattr(builtins, "ExceptionGroup"):
            self.skipTest("ExceptionGroup needs Python 3.11+")
        request = httpx.Request("GET", "http://hub/mcp")
        response = httpx.Response(401, request=request)
        status_error = httpx.HTTPStatusError(
            "unauthorized", request=request, response=response)
        group = builtins.ExceptionGroup("conn", [status_error])
        wrapper = RuntimeError("task group failed")
        wrapper.__cause__ = group
        self.assertEqual(
            self.transport._knowledge_refusal_code(wrapper),
            "authorization_denied")
        ok_response = httpx.Response(500, request=request)
        other = httpx.HTTPStatusError(
            "boom", request=request, response=ok_response)
        self.assertIsNone(self.transport._knowledge_refusal_code(other))

    def test_knowledge_pointer_maps_typed_refusal_not_empty_pool(self):
        def call_fn(url, token, tool, args):
            async def _refuse():
                raise self._Refused("namespace_not_allowed")
            return _refuse()

        http = self.transport.McpHttp(
            url="http://127.0.0.1:9/mcp",
            executor=self.transport.DeadlineExecutor(),
            call_fn=call_fn, deadline_s=5.0)
        result = http.knowledge_pointer("notes", "run/a",
                                        namespace="user:global")
        self.assertEqual(result, {"ok": False,
                                  "reason": "namespace_not_allowed"})

    def test_apply_knowledge_ops_maps_typed_refusal(self):
        def call_fn(url, token, tool, args):
            async def _refuse():
                raise self._Refused("capability_denied")
            return _refuse()

        http = self.transport.McpHttp(
            url="http://127.0.0.1:9/mcp",
            executor=self.transport.DeadlineExecutor(),
            call_fn=call_fn, deadline_s=5.0)
        result = http.apply_knowledge_ops([_link_op("kl-1")])
        self.assertEqual(result, {"ok": False,
                                  "reason": "capability_denied"})


class EngineAdmissionTests(unittest.TestCase):
    """The ops engine's knowledge_link admission (the SAME engine the hub
    runs): namespace first, closed whitelist, capability split."""

    def _apply(self, env, ops, principal=None):
        conn = _open_rw(env.store_path)
        try:
            return opslib.apply_ops(
                conn, ops, default_principal=principal
                or LegacyPrincipal())
        finally:
            conn.close()

    def test_whitelist_violations_are_bad_op(self):
        env = _Env()
        closed = {"namespace", "target", "key", "version", "locator",
                  "fullhash", "summary", "date", "expected_row_id"}
        cases = [
            _link_op("kl-x", extra={"content": "note body"}),
            _link_op("kl-x", extra={"path": "C:/vault/note.md"}),
            _link_op("kl-x", extra={"source_ref": "knowledge:notes:run/a"}),
        ]
        for op in cases:
            with self.subTest(extra=sorted(set(op["args"]) - closed)):
                result = self._apply(env, [op])
                self.assertEqual(result["receipts"][0]["status"],
                                 "rejected")
                self.assertEqual(result["receipts"][0]["error"], "bad_op")
        # malformed chunked digest / path-shaped locator / bad version
        for mutate in (
            lambda a: a.update({"fullhash": ["aaaaaaaa"] * 7}),
            lambda a: a.update({"locator": r"C:\vault\note.md"}),
            lambda a: a.update({"version": 0}),
            lambda a: a.update({"expected_row_id": ""}),
            lambda a: a.update({"summary": ""}),
        ):
            op = _link_op("kl-y")
            mutate(op["args"])
            result = self._apply(env, [op])
            self.assertEqual(result["receipts"][0]["error"], "bad_op",
                             "args after mutate: %r" % op["args"])

    def test_capability_split_write_vs_mutate(self):
        env = _Env()
        writer = _Principal(frozenset({"write"}))
        mutator = _Principal(frozenset({"mutate"}))
        reader = _Principal(frozenset({"read"}))

        # First create: write is enough.
        result = self._apply(env, [_link_op("kl-c1")], principal=writer)
        self.assertEqual(result["receipts"][0]["status"], "committed")
        self.assertEqual(result["receipts"][0]["link_status"], "created")

        # Idempotent noop: same version+digest, staked on the LIVE row
        # (link_pointer's law: an op without a stake against an existing
        # pointer is an initial-create refusal, never a silent noop — the
        # workflow's sidecar carries the observed row id for redelivery).
        # A no-stake redelivery is an honest pointer_cas_stale.
        result = self._apply(env, [_link_op("kl-c2")], principal=writer)
        self.assertEqual(result["receipts"][0]["error"],
                         "pointer_cas_stale")
        from storelib import knowledge as _sk
        conn = _open_rw(env.store_path)
        try:
            live = _sk.get_pointer(conn, "user:global", "notes", "run/a")
        finally:
            conn.close()
        self.assertIsNotNone(live)
        # ANY supplied expected_row_id is conservatively MUTATE (the CAS
        # stake names the row a replacement would tombstone) — so the
        # staked noop redelivery belongs to the mutator, not the writer.
        result = self._apply(env, [
            _link_op("kl-c2b", expected=live.row_id)], principal=writer)
        self.assertEqual(result["receipts"][0]["error"],
                         "capability_denied")
        result = self._apply(env, [
            _link_op("kl-c2b", expected=live.row_id)], principal=mutator)
        self.assertEqual(result["receipts"][0]["status"], "committed")
        self.assertEqual(result["receipts"][0]["link_status"], "noop")
        self.assertTrue(result["receipts"][0]["deduped"])

        # Replacement (advance, no expected_row_id): requires mutate.
        v2 = _link_op("kl-c3", version=2, text="advance body\n")
        result = self._apply(env, [v2], principal=writer)
        self.assertEqual(result["receipts"][0]["status"], "rejected")
        self.assertEqual(result["receipts"][0]["error"],
                         "capability_denied")
        # The capability gate PASSED for the mutator (capability_denied is
        # the WRITER's verdict above); the handler then refuses the advance
        # because link_pointer requires an explicit CAS stake for ANY
        # replacement — an honest pointer_cas_stale, never a silent win.
        result = self._apply(env, [v2], principal=mutator)
        self.assertEqual(result["receipts"][0]["status"], "rejected")
        self.assertEqual(result["receipts"][0]["error"],
                         "pointer_cas_stale")

        # ANY supplied expected_row_id: conservatively mutate.
        conn = _open_rw(env.store_path)
        from storelib import knowledge as store_knowledge
        pointer = store_knowledge.get_pointer(
            conn, "user:global", "notes", "run/a")
        conn.close()
        self.assertIsNotNone(pointer)
        cas = _link_op("kl-c4", version=3, text="cas body\n",
                       expected=pointer.row_id)
        result = self._apply(env, [cas], principal=writer)
        self.assertEqual(result["receipts"][0]["error"],
                         "capability_denied")

        # read-only: even a first create denies (fresh identity).
        result = self._apply(env, [_link_op("kl-c5", key="other/x")],
                             principal=reader)
        self.assertEqual(result["receipts"][0]["error"],
                         "capability_denied")

    def test_namespace_scope_checked_first(self):
        env = _Env()
        scoped = _Principal(frozenset({"write", "mutate"}))
        scoped.scoped = True
        scoped.namespaces = frozenset({"proj:mine"})
        op = _link_op("kl-ns", namespace="proj:other")
        result = self._apply(env, [op], principal=scoped)
        self.assertEqual(result["receipts"][0]["error"],
                         "namespace_not_allowed")

    def test_unknown_principal_denied_for_every_kind(self):
        env = _Env()
        lookup = opslib._PrincipalIndex({"principals": []})
        conn = _open_rw(env.store_path)
        try:
            result = opslib.apply_ops(
                conn, [_link_op("kl-up")], principal="ghost",
                principal_lookup=lookup)
        finally:
            conn.close()
        self.assertEqual(result["receipts"][0]["error"], "unknown_principal")

    def test_stale_cas_and_conflict_receipts(self):
        env = _Env()
        self._apply(env, [_link_op("kl-s1")])
        conn = _open_rw(env.store_path)
        from storelib import knowledge as store_knowledge
        pointer = store_knowledge.get_pointer(
            conn, "user:global", "notes", "run/a")
        conn.close()
        # Wrong stake: stale CAS.
        result = self._apply(env, [_link_op(
            "kl-s2", version=2, text="v2 body\n",
            expected="00000000-0000-0000-0000-000000000000")])
        self.assertEqual(result["receipts"][0]["error"],
                         "pointer_cas_stale")
        # Same version, different digest: immutability conflict.
        result = self._apply(env, [_link_op(
            "kl-s3", version=1, text="other body\n",
            expected=pointer.row_id)])
        self.assertEqual(result["receipts"][0]["error"],
                         "pointer_conflict")
        # Version regression: stale.
        doc_v_low = prepare_document("notes", "run/a", "low body\n")
        result = self._apply(env, [_link_op(
            "kl-s4", version=1, text="regress body\n",
            expected=pointer.row_id)])
        self.assertIn(result["receipts"][0]["error"],
                      ("pointer_cas_stale", "pointer_conflict"))
        # Valid CAS advance commits with link_status advanced.
        result = self._apply(env, [_link_op(
            "kl-s5", version=2, text="real v2 body\n",
            expected=pointer.row_id)])
        self.assertEqual(result["receipts"][0]["status"], "committed")
        self.assertEqual(result["receipts"][0]["link_status"], "advanced")


class QueueIsolationTests(unittest.TestCase):
    def test_knowledge_never_touches_core_opqueue(self):
        env = _Env()
        hub = _EngineHub(env.store_path, apply_mode="fail")
        workflow.capture(
            target_id="notes", logical_key="run/a", text=NOTE,
            mode="remote", config=env.config(), journal=env.journal,
            delivery=env.delivery, http=hub)
        # The CORE op queue file was never created by the knowledge lane.
        from fleet import opqueue
        core_path = opqueue.queue_path(
            "local", env={**os.environ, "ZMEM_DATA": env.tmp})
        self.assertFalse(Path(core_path).exists(),
                         "the knowledge lane must never create or touch "
                         "the core op queue (got %s)" % core_path)
        # And the knowledge sidecar carries only knowledge_link entries.
        snapshot = env.delivery.snapshot()
        self.assertTrue(snapshot["queue"])
        self.assertTrue(all(
            build_link_op(e)["op"] == "knowledge_link"
            for e in snapshot["queue"]))

    def test_core_ops_never_carry_knowledge_kind(self):
        """parse_ops_line accepts knowledge_link only as its OWN kind;
        the core ROUTE-WRITE band never maps a knowledge op."""
        from storelib.remote_gate import _ROUTE_WRITE
        self.assertNotIn("knowledge", _ROUTE_WRITE)
        self.assertNotIn("knowledge_link", _ROUTE_WRITE)
        line = json.dumps(_link_op("kl-parse"))
        parsed = opslib.parse_ops_line(line)
        self.assertEqual(parsed["op"], "knowledge_link")


def _all_tests():
    return unittest.defaultTestLoader.loadTestsFromModule(
        sys.modules[__name__])


if __name__ == "__main__":
    unittest.main(verbosity=2)
