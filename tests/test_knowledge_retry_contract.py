"""Arm's-length acceptance checks: the ``knowledge retry`` contract
(issue trace 480-481-482, rebuild r3 per critic round 2).

Published selectors (RetryContractTest): C1-C4 plus supplementary
negatives.  The C1 fixture is the critic-endorsed INITIAL-CREATE stale-CAS
path, built through REAL captures:

    1. a competing first-create (real capture of alpha') wins the pointer;
    2. beta's crashed INITIAL stake (None, 1) is seeded in the delivery
       intent (the documented crash-recovery sidecar);
    3. a REAL capture of beta publishes beta's immutable document and its
       link is refused ("initial link refused: a verified pointer already
       exists") -> journal entry QUARANTINED cas_stale, stake preserved;
    4. the competing pointer is tombstoned through the REAL maintenance
       API (storelib.knowledge.quarantine_pointer_row);
    5. retrying the UNCHANGED (None, 1) stake succeeds: no live verified
       pointer exists (the pre-retry live identity is asserted), so the
       initial create is genuinely available again.

Frozen retry contract (normative; the implementer must satisfy exactly):
- CLI ``knowledge retry --target T --key K [--namespace N] [--json]``.
- CURRENT for an INITIAL-CREATE stake (expected_row_id None, version 1)
  := no live verified pointer for the identity.  On CURRENT: exit 0,
  ``{"ok": true, ...}``, the OLD quarantined journal entry retained
  VERBATIM (full entry-dict equality), a NEW higher-seq journal entry for
  the same digest, a FRESH non-None op id in the delivery sidecar, the
  pointer created at the original version with the original digest, and
  a readback of the target verifying the staked bytes.  No stake
  component (expected_row_id/version/digest/locator) is ever refreshed.
- On STALE (advance stakes; frozen scenario version 7, expected_row_id
  ``kl-arm-length-stale-row-0000``): exit EXACTLY 1,
  ``{"ok": false, "error": ...}`` naming the ORIGINAL stake version;
  journal.json + delivery.json + the pointer row byte-identical.
- Latest-version selection: multiple quarantined versions of one key ->
  the retry selects the LATEST cas_stale entry (its digest lands).
- Malformed journal/intent -> typed refusal (exit 1), never a traceback.
- Concurrent retries: exactly one succeeds at linking (FB-002) — the
  winner links (``applied``), the loser's stale CAS reconciles against
  the landed pointer (``noop`` + ``reconciled_after_cas_stale``) and both
  report ok; the pointer row is still linked exactly once.
- Crash/partial failure: a journal entry recorded but pointer not
  advanced -> a fresh retry re-selects and completes.
- Target tamper: staked bytes altered on the target -> typed refusal,
  no link (no pointer row created).
- Kill switch ZMEM_CAPTURE=0 gates retry BEFORE any sidecar/store open;
  disabled config refuses; no-quarantined-intent refuses; second retry
  after success refuses.
- REMOTE: with a REACHABLE hub advertising knowledge_link_v1 (the
  in-process fake MCP hub below), retry ENQUEUES a FRESH op carrying the
  ORIGINAL stake verbatim (expected_row_id + version + digest preserved,
  queue status "queued" — the flush lane applies LATER; nothing asserts
  "applied"), keeps the old receipt and unrelated queue entries
  byte-identical, exits 0 with ``{"ok": true, "queued": true}``; with an
  UNREACHABLE hub, exit 3 pending envelope, NOTHING enqueued
  (delivery.json byte-identical).

Base: every selector except C4 is RED via ``invalid choice: 'retry'``
(the preconditions are all real-path built and asserted BEFORE the retry
call); C4 is GREEN at base.

Run: python -X utf8 tests/test_knowledge_retry_contract.py
"""

from __future__ import annotations
import os
import sys
import tempfile

# Scratch env pinned BEFORE the isolation guard and any storelib import.
_TMP = tempfile.mkdtemp(prefix="zmem-knretry-mod-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ.setdefault("ZMEM_MODELS_DIR", os.path.join(_TMP, "no-models"))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

import hashlib
import http.server
import json
import shutil
import sqlite3
import subprocess
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

_TARGET = "notes"
_KEY = "guides/deploy"
_NAMESPACE = "user:global"
_STALE_VERSION = 7
# The EXPLICIT --summary the fixtures pass at capture: C1's provenance
# exactness asserts the original (and the retried) journal record carries
# THIS EXACT string.
_EXPLICIT_SUMMARY = "fleet deploy runbook summary"


def _digest(text: str) -> str:
    canon = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _locator(key: str, digest: str) -> str:
    seg = "-".join(digest[i:i + 16] for i in range(0, 64, 16))
    return "%s/%s.md" % (key, seg)


def _canonical(text: str) -> bytes:
    canon = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    return canon.encode("utf-8")


# ---------------------------------------------------------------------------
# In-process fake MCP hub (StreamableHTTP): advertises knowledge_link_v1,
# applies every knowledge_link op as committed, records the ops it saw.


class _FakeMCPHub(http.server.BaseHTTPRequestHandler):
    seen_ops: list[dict] = []
    tool_calls: list[str] = []     # every tools/call NAME received
    token: str = "zmem-retry-fake-hub-token"
    # FB-017 fixtures: when reject_error is set, every knowledge_link op
    # is answered with status "rejected" carrying that machine error
    # code; hub_pointer_answer is what the capability-gated
    # knowledge_pointer read returns (None = the hub holds no verified
    # pointer, so a pointer_cas_stale rejection never reconciles as
    # landed).
    reject_error: str | None = None
    hub_pointer_answer: dict | None = None

    def log_message(self, *args):  # silence
        pass

    @classmethod
    def reset(cls) -> None:
        _FakeMCPHub.seen_ops = []
        _FakeMCPHub.tool_calls = []
        _FakeMCPHub.reject_error = None
        _FakeMCPHub.hub_pointer_answer = None

    def _send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - no server-initiated stream
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        msg = json.loads(self.rfile.read(length).decode("utf-8"))
        method = msg.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        reply = {"jsonrpc": "2.0", "id": msg.get("id")}
        if method == "initialize":
            reply["result"] = {
                "protocolVersion": msg["params"].get("protocolVersion",
                                                     "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-hub", "version": "0"},
            }
        elif method == "tools/list":
            reply["result"] = {"tools": [
                {"name": n, "inputSchema": {"type": "object"}}
                for n in ("hello", "apply_ops", "knowledge_pointer")]}
        elif method == "tools/call":
            name = msg["params"]["name"]
            _FakeMCPHub.tool_calls.append(name)
            args = msg["params"].get("arguments") or {}
            if name == "hello":
                payload = {"ok": True, "protocol": 1,
                           "capabilities": ["knowledge_link_v1"],
                           "zmem_version": "retry-fake-hub"}
            elif name == "knowledge_pointer":
                payload = {"ok": True,
                           "pointer": _FakeMCPHub.hub_pointer_answer}
            elif name == "apply_ops":
                _FakeMCPHub.seen_ops.append(json.loads(json.dumps(args)))
                if _FakeMCPHub.reject_error is not None:
                    payload = {"ok": True, "receipts": [
                        {"op_id": op["op_id"], "status": "rejected",
                         "error": _FakeMCPHub.reject_error}
                        for op in args.get("ops", [])]}
                else:
                    payload = {"ok": True, "receipts": [
                        {"op_id": op["op_id"], "status": "committed",
                         "canonical_id": "row-" + op["op_id"]}
                        for op in args.get("ops", [])]}
            else:
                payload = {"ok": True}
            reply["result"] = {"content": [
                {"type": "text", "text": json.dumps(payload)}],
                "isError": False}
        else:
            reply["result"] = {}
        self._send_json(200, reply)


class _FakeHubServer:
    def __init__(self, *, reject_error: str | None = None,
                 hub_pointer_answer: dict | None = None) -> None:
        _FakeMCPHub.reset()
        _FakeMCPHub.reject_error = reject_error
        _FakeMCPHub.hub_pointer_answer = hub_pointer_answer
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakeMCPHub)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/mcp" % self.httpd.server_address[1]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------


class RetryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knretry-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self._env(remote=False)
        r = self._cli("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.tmp / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                _TARGET: {"kind": "markdown", "root": str(self.vault)}}}
        }), encoding="utf-8")

    def _env(self, *, remote: bool, remote_url: str | None = None,
             capture: str | None = None):
        env = {**os.environ}
        if remote:
            env.pop("ZMEM_STORE", None)
            env["ZMEM_DATA"] = str(self.tmp).replace("\\", "/")
            env["ZMEM_REMOTE_URL"] = remote_url or \
                "http://127.0.0.1:9/no-such-hub"
            env["ZMEM_MCP_TOKEN"] = _FakeMCPHub.token
        else:
            env["ZMEM_STORE"] = str(self.tmp / "store.sqlite").replace("\\", "/")
            env["ZMEM_DATA"] = str(self.tmp).replace("\\", "/")
            env.pop("ZMEM_REMOTE_URL", None)
            env.pop("ZMEM_MCP_TOKEN", None)
        env["ZMEM_MODELS_DIR"] = str(self.tmp / "no-models")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        env.pop("ZMEM_CAPTURE", None)
        if capture is not None:
            env["ZMEM_CAPTURE"] = capture
        self.env = env

    # -- subprocess helpers -------------------------------------------------

    def _cli(self, *args: str, env=None, timeout: float = 180.0):
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env if env is None else env,
            cwd=str(REPO_ROOT), timeout=timeout)

    def _popen(self, *args: str, env=None):
        return subprocess.Popen(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            env=self.env if env is None else env, cwd=str(REPO_ROOT))

    def _capture(self, key: str, text: str) -> dict:
        r = self._cli("knowledge", "capture", "--target", _TARGET,
                      "--key", key, "--namespace", _NAMESPACE,
                      "--content", text, "--json")
        self.assertEqual(r.returncode, 0,
                         "capture %s failed: %s%s" % (key, r.stdout, r.stderr))
        return json.loads(r.stdout)

    def _capture_quarantined(self, key: str, text: str,
                             summary: str | None = _EXPLICIT_SUMMARY) -> dict:
        """A DESIGNED-TO-QUARANTINE capture: the genuine stale-CAS
        quarantine envelope — exit 1 (the CLI's pointer-failure exit),
        ``pointer.status == "cas_stale"`` and ``journal_state ==
        "quarantined"``.  An EXPLICIT --summary rides the capture so the
        journal record the REAL capture writes (at head) carries
        provenance the retry must copy verbatim; ``summary=None`` omits
        --summary entirely (the FB-001 default-summary probe)."""
        cli_args = ["knowledge", "capture", "--target", _TARGET,
                    "--key", key, "--namespace", _NAMESPACE,
                    "--content", text]
        if summary is not None:
            cli_args += ["--summary", summary]
        cli_args += ["--json"]
        r = self._cli(*cli_args)
        self.assertEqual(
            r.returncode, 1,
            "the designed-to-quarantine capture of %r must exit 1 with the "
            "quarantine envelope: %s%s" % (key, r.stdout, r.stderr))
        receipt = json.loads(r.stdout)
        self.assertEqual(
            (receipt.get("pointer") or {}).get("status"), "cas_stale",
            "the stale-CAS envelope is expected: %r" % (receipt,))
        self.assertEqual(receipt.get("journal_state"), "quarantined",
                         "the attempt is terminally quarantined: %r"
                         % (receipt,))
        self.assertTrue(receipt.get("doc_published"),
                        "the immutable document IS published: %r"
                        % (receipt,))
        return receipt

    def _retry(self, key: str = _KEY, *, env=None):
        return self._cli("knowledge", "retry", "--target", _TARGET,
                         "--key", key, "--namespace", _NAMESPACE, "--json",
                         env=env)

    def _retry_popen(self, key: str = _KEY):
        return self._popen("knowledge", "retry", "--target", _TARGET,
                           "--key", key, "--namespace", _NAMESPACE, "--json")

    def _read(self, key: str, locator: str):
        return self._cli("knowledge", "read", "--target", _TARGET,
                         "--key", key, "--locator", locator,
                         "--namespace", _NAMESPACE, "--json")

    # -- sidecars ------------------------------------------------------------

    @property
    def _journal_path(self) -> Path:
        return self.tmp / "knowledge" / "journal.json"

    @property
    def _delivery_path(self) -> Path:
        return self.tmp / "knowledge" / "delivery.json"

    def _journal(self) -> dict:
        if not self._journal_path.exists():
            return {"entries": []}
        return json.loads(self._journal_path.read_text(encoding="utf-8"))

    def _delivery(self) -> dict:
        if not self._delivery_path.exists():
            # Documented sidecar shape (delivery.py _DELIVERY_VERSION=1):
            # the version key is REQUIRED by _read; a seeded sidecar
            # without it is refused as unrecognized.
            return {"version": 1, "queue": [], "receipts": {},
                    "intents": {}}
        return json.loads(self._delivery_path.read_text(encoding="utf-8"))

    def _seed_stake(self, key: str, text: str, expected_row_id, version: int,
                    state: str = "staked") -> dict:
        """Seed ONLY the crashed writer's CAS stake (the delivery intent
        sidecar, documented crash-recovery format).  Retry metadata
        (summary / staked_at) is NOT injected here anymore (r6): the REAL
        capture path persists it on the journal record itself."""
        dg = _digest(text)
        ref = "knowledge:%s:%s" % (_TARGET, key)
        delivery = self._delivery()
        delivery["intents"]["%s|%s" % (_NAMESPACE, ref)] = {
            "target": _TARGET, "key": key, "namespace": _NAMESPACE,
            "digest": dg, "locator": _locator(key, dg), "version": version,
            "expected_row_id": expected_row_id, "row_id": None,
            "state": state, "op_id": None, "staked": True, "ref": ref,
            "updated_at": "2026-01-01T00:00:00Z",
        }
        self._delivery_path.parent.mkdir(parents=True, exist_ok=True)
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")
        return {"digest": dg, "locator": _locator(key, dg)}

    def _intent(self, key: str):
        ref = "knowledge:%s:%s" % (_TARGET, key)
        return self._delivery()["intents"].get("%s|%s" % (_NAMESPACE, ref))

    def _entries_for(self, key: str):
        ref = "knowledge:%s:%s" % (_TARGET, key)
        return [e for e in self._journal()["entries"] if e["ref"] == ref]

    def _op_ids(self) -> set:
        d = self._delivery()
        ids = {e["op_id"] for e in d["queue"] if e.get("op_id")}
        ids.update(k for k in d["receipts"] if k)
        for rec in d["intents"].values():
            if isinstance(rec, dict) and rec.get("op_id"):
                ids.add(rec["op_id"])
        return ids

    def _pointer_rows(self, key: str = _KEY):
        if not (self.tmp / "store.sqlite").exists():
            return []
        conn = sqlite3.connect(str(self.tmp / "store.sqlite"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, namespace, content, superseded_at, supersede_reason "
            "FROM memory WHERE superseded_at IS NULL AND source_ref=?",
            ("knowledge:%s:%s" % (_TARGET, key),)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # -- the r3 INITIAL-CREATE fixture --------------------------------------

    def _fixture_initial_create_quarantine(self, key: str = _KEY,
                                           text: str = "beta body") -> dict:
        """Real-path quarantine: a competing first-create wins the pointer;
        beta's crashed (None, 1) initial stake makes its REAL capture
        publish the document and quarantine cas_stale."""
        # A competing writer's first create wins the pointer (a repeated
        # fixture on the same scratch re-delivers the same immutable
        # version -> "noop", same row).
        competitor = self._capture(key, "alpha prime body")
        self.assertIn(competitor["pointer"]["status"], ("created", "noop"))
        row_id = competitor["pointer"]["row_id"]

        # Beta's crashed INITIAL stake (None, 1) in the delivery intent.
        seeded = self._seed_stake(key, text, None, 1)

        # The REAL capture of beta: document published, link refused —
        # the designed-to-quarantine capture (exit 1, cas_stale envelope).
        receipt = self._capture_quarantined(key, text)

        # beta's immutable document IS published and readback-verified.
        rd = self._read(key, seeded["locator"])
        self.assertEqual(rd.returncode, 0, rd.stdout + rd.stderr)
        read = json.loads(rd.stdout)
        self.assertTrue(read.get("ok"))
        self.assertEqual(read["actual_digest"], seeded["digest"])

        quarantined = [e for e in self._entries_for(key)
                       if e["digest"] == seeded["digest"]
                       and e["state"] == "quarantined"]
        self.assertTrue(quarantined, "the journal entry is quarantined")
        return {"digest": seeded["digest"], "locator": seeded["locator"],
                "entry": quarantined[0], "seq": quarantined[0]["seq"],
                "competitor_row": row_id}

    def _tombstone_competitor(self, row_id: str) -> None:
        """The real maintenance lane: the sanctioned single-row tombstone."""
        old = os.environ.get("ZMEM_STORE")
        os.environ["ZMEM_STORE"] = str(self.tmp / "store.sqlite")
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            import storelib  # noqa: PLC0415
            storelib._refresh_env_state()
            from storelib import knowledge as sk  # noqa: PLC0415
            conn = sqlite3.connect(str(self.tmp / "store.sqlite"))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                ok = sk.quarantine_pointer_row(
                    conn, row_id,
                    reason="arm-length probe: rollback of the competing "
                           "first-create")
                conn.commit()
                self.assertTrue(ok)
            finally:
                conn.close()
        finally:
            if old is None:
                os.environ.pop("ZMEM_STORE", None)
            else:
                os.environ["ZMEM_STORE"] = old

    def _live_pointer(self):
        rows = self._pointer_rows()
        return rows[0] if rows else None

    def _link_other_writer(self, *, version: int, digest: str,
                           summary: str) -> None:
        """Another writer's link DIRECTLY on the shared store (past this
        host's journal/delivery sidecars, which keep carrying THIS host's
        stake verbatim) — the real maintenance lane, mirroring
        ``_tombstone_competitor``."""
        old = os.environ.get("ZMEM_STORE")
        os.environ["ZMEM_STORE"] = str(self.tmp / "store.sqlite")
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            import storelib  # noqa: PLC0415
            storelib._refresh_env_state()
            from storelib import knowledge as sk  # noqa: PLC0415
            conn = sqlite3.connect(str(self.tmp / "store.sqlite"))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                sk.link_pointer(
                    conn, namespace=_NAMESPACE, target=_TARGET, key=_KEY,
                    version=version, full_sha256=digest, summary=summary,
                    locator=_locator(_KEY, digest), expected_row_id=None)
                conn.commit()
            finally:
                conn.close()
        finally:
            if old is None:
                os.environ.pop("ZMEM_STORE", None)
            else:
                os.environ["ZMEM_STORE"] = old

    # -- C1 ------------------------------------------------------------------

    def test_retry_on_current_original_stake_succeeds(self):
        fx = self._fixture_initial_create_quarantine()
        old_entry_verbatim = json.loads(json.dumps(fx["entry"]))
        op_ids_before = self._op_ids()
        receipt_keys_before = set(self._delivery()["receipts"])
        # PROVENANCE EXACTNESS (r7/r8, asserted BEFORE the retry): the
        # REAL capture (with the explicit --summary) persists the EXACT
        # summary string and a staked-at date on the ORIGINAL journal
        # record — equality with the explicit string, not mere non-None
        # (no vacuous linkage downstream).
        self.assertEqual(
            old_entry_verbatim.get("summary"), _EXPLICIT_SUMMARY,
            "the capture persists the summary on the ORIGINAL journal "
            "entry (source metadata present; got %r — the capture-side "
            "provenance write is missing or wrong)"
            % (old_entry_verbatim.get("summary"),))
        self.assertIsNotNone(
            old_entry_verbatim.get("staked_at"),
            "the capture persists the staked-at date on the ORIGINAL "
            "journal entry (source metadata present; got None)")

        # Tombstone the competing pointer: the pre-retry live identity is
        # NO live verified pointer — the initial-create stake is CURRENT.
        self._tombstone_competitor(fx["competitor_row"])
        self.assertIsNone(
            self._live_pointer(),
            "PRE-RETRY live identity: no live row after the tombstone")

        result = self._retry()
        self.assertEqual(result.returncode, 0,
                         "stdout=%r stderr=%r" % (result.stdout[-400:],
                                                  result.stderr[-400:]))
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt.get("ok"), receipt)

        # The OLD quarantined journal entry survives VERBATIM (full dict
        # equality — not selected fields).
        entries = self._entries_for(_KEY)
        same = [e for e in entries if e["seq"] == fx["seq"]]
        self.assertEqual(same, [old_entry_verbatim],
                         "the ORIGINAL quarantined entry must be retained "
                         "verbatim (full entry equality)")

        # A NEW higher-seq journal operation for the same digest,
        # enumerated from the JOURNAL entries (the same journal read that
        # produced the verbatim-preservation list above) — NOT from the
        # delivery sidecar.
        fresh = [e for e in entries if e["seq"] > fx["seq"]
                 and e["digest"] == fx["digest"]]
        # FRESH JOURNAL EXACTNESS (r9, critic round 8): EXACTLY ONE fresh
        # same-digest journal entry above the original seq, and that entry
        # is TERMINAL ("applied") — asserted BEFORE any metadata/linkage
        # read below.  The delivery-side fresh_ids == {new_op_id} set
        # equality further down inspects _op_ids() (delivery queue/
        # receipts/intents) ONLY: duplicate journal entries or a leading
        # doc_pending entry can share one delivery op id and pass there,
        # so the journal shape is pinned here (both must hold).
        self.assertEqual(
            len(fresh), 1,
            "exactly one fresh terminal journal entry (applied): got %d "
            "fresh same-digest journal entries above seq=%r — %r"
            % (len(fresh), fx["seq"],
               [(e.get("seq"), e.get("state")) for e in fresh]))
        self.assertEqual(
            fresh[0].get("state"), "applied",
            "exactly one fresh terminal journal entry (applied): the "
            "single fresh same-digest entry (seq=%r) has state=%r"
            % (fresh[0].get("seq"), fresh[0].get("state")))
        # Retry provenance LINKED, not hardcoded (r6): the NEW entry's
        # summary/staked_at EQUAL the ORIGINAL quarantined entry's — the
        # REAL capture persisted them on the original record (explicit
        # --summary) and the retry copied them verbatim at retry time.
        original_entry = old_entry_verbatim
        self.assertEqual(
            fresh[0].get("summary"), original_entry.get("summary"),
            "the NEW entry's summary EQUALS the ORIGINAL entry's (copied "
            "verbatim at retry time): new=%r original=%r"
            % (fresh[0].get("summary"), original_entry.get("summary")))
        self.assertEqual(
            fresh[0].get("staked_at"), original_entry.get("staked_at"),
            "the NEW entry's staked_at EQUALS the ORIGINAL entry's "
            "(copied verbatim at retry time): new=%r original=%r"
            % (fresh[0].get("staked_at"),
               original_entry.get("staked_at")))
        # FROZEN LINKAGE SHAPE (r6/r7): the fresh receipt is keyed by the
        # NEW entry's op_id, and the new entry's retry_ref names the
        # ORIGINAL quarantined entry's seq.
        new_op_id = fresh[0].get("op_id")
        self.assertTrue(new_op_id,
                        "the NEW journal entry carries a non-None op_id")
        # FRESH-OP-ID IDENTITY (r7): nonempty AND different from the
        # ORIGINAL entry's op id AND from every pre-existing receipt key.
        self.assertNotEqual(
            new_op_id, old_entry_verbatim.get("op_id"),
            "the new op id DIFFERS from the original entry's op id")
        self.assertNotIn(
            new_op_id, receipt_keys_before,
            "the new op id differs from every pre-existing receipt key")
        receipts_now = self._delivery()["receipts"]
        self.assertIn(
            new_op_id, receipts_now,
            "LINKAGE: the receipt key == the new entry's op_id (%r)"
            % (new_op_id,))
        self.assertEqual(
            receipts_now[new_op_id].get("op_id"), new_op_id,
            "the receipt dict under the key carries the linkage (the "
            "receipt's op id == the new entry's op_id)")
        self.assertEqual(
            fresh[0].get("retry_ref"), fx["seq"],
            "LINKAGE: the new entry's retry_ref names the ORIGINAL "
            "quarantined entry's seq (retry_ref=%r expected=%r)"
            % (fresh[0].get("retry_ref"), fx["seq"]))

        # FRESH-OP-ID EXACTNESS (r8): the set of NEW op ids created by
        # the retry is EXACTLY {new_op_id} — set equality, not nonempty
        # (exactly one terminal retry entry besides the original).
        fresh_ids = {i for i in self._op_ids() - op_ids_before if i}
        self.assertEqual(
            fresh_ids, {new_op_id},
            "the retry created EXACTLY ONE new op id (fresh_ids=%r "
            "expected={%r}; before=%r)"
            % (sorted(fresh_ids), new_op_id, sorted(op_ids_before)))

        # NO stake component was refreshed: the recorded stake keeps the
        # ORIGINAL expected_row_id (None) and version (1) and digest.
        intent = self._intent(_KEY)
        self.assertEqual(intent.get("expected_row_id"), None,
                         "the original initial-create stake (None) is "
                         "never refreshed: %r" % (intent,))
        self.assertEqual(intent.get("version"), 1)
        self.assertEqual(intent.get("digest"), fx["digest"])
        self.assertEqual(intent.get("locator"), fx["locator"])

        # The pointer was created at the original version with the
        # original digest, and the staked bytes read back verified.
        rows = self._pointer_rows()
        self.assertEqual(len(rows), 1, "exactly one live pointer row")
        status = self._cli("knowledge", "status", "--target", _TARGET,
                           "--key", _KEY, "--namespace", _NAMESPACE, "--json")
        pointer = json.loads(status.stdout)["pointers"][0]["pointer"]
        self.assertEqual(pointer["version"], 1)
        self.assertEqual(pointer["full_sha256"], fx["digest"])
        rd = self._read(_KEY, fx["locator"])
        self.assertEqual(json.loads(rd.stdout)["actual_digest"],
                         fx["digest"])

    # -- FB-001: the default summary when NO --summary was passed -----------

    def test_retry_without_summary_lands_default_pointer_summary(self):
        """A capture made WITHOUT --summary quarantines with a NULL
        summary on the journal entry; the completing retry must fall
        back to the FIXED default ``knowledge note <logical_key>`` (the
        logical-key form, never note content) instead of raising
        NameError (FB-001)."""
        competitor = self._capture(_KEY, "alpha prime body")
        seeded = self._seed_stake(_KEY, "beta body", None, 1)
        # The designed-to-quarantine capture with NO --summary flag.
        self._capture_quarantined(_KEY, "beta body", summary=None)
        # The quarantined journal entry carries NO summary.
        quarantined = [e for e in self._entries_for(_KEY)
                       if e["digest"] == seeded["digest"]
                       and e["state"] == "quarantined"]
        self.assertTrue(quarantined)
        self.assertIsNone(quarantined[0].get("summary"),
                          "the no-summary capture records a NULL summary")

        self._tombstone_competitor(competitor["pointer"]["row_id"])
        result = self._retry()
        self.assertEqual(result.returncode, 0,
                         "stdout=%r stderr=%r" % (result.stdout[-300:],
                                                  result.stderr[-300:]))
        body = json.loads(result.stdout)
        self.assertTrue(body.get("ok"), body)

        # The landed POINTER carries the FIXED default summary.
        status = self._cli("knowledge", "status", "--target", _TARGET,
                           "--key", _KEY, "--namespace", _NAMESPACE,
                           "--json")
        pointer = json.loads(status.stdout)["pointers"][0]["pointer"]
        self.assertEqual(
            pointer["summary"], "knowledge note %s" % _KEY,
            "the completing retry links the FIXED default summary "
            "(knowledge note <logical_key>): %r" % (pointer["summary"],))
        self.assertEqual(pointer["full_sha256"], seeded["digest"])

    # -- C2 ------------------------------------------------------------------

    def test_retry_on_stale_stake_refuses_without_cas_refresh(self):
        self._capture(_KEY, "alpha body")
        self._capture(_KEY, "alpha two body")          # pointer at v2
        seeded = self._seed_stake(_KEY, "beta body",
                                  "kl-arm-length-stale-row-0000",
                                  _STALE_VERSION, state="cas_stale")
        # The designed-to-quarantine capture (exit 1, cas_stale envelope).
        self._capture_quarantined(_KEY, "beta body")
        journal_before = self._journal_path.read_bytes()
        delivery_before = self._delivery_path.read_bytes()
        pointer_before = self._pointer_rows()

        result = self._retry()
        self.assertEqual(result.returncode, 1,
                         "a still-STALE stake must exit EXACTLY 1; got %d "
                         "(stdout=%r stderr=%r)"
                         % (result.returncode, result.stdout,
                            result.stderr[-300:]))
        body = json.loads(result.stdout)
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", ""))
        self.assertIn("version", error.lower())
        self.assertIn(str(_STALE_VERSION), error)
        intent = self._intent(_KEY)
        self.assertEqual(intent.get("expected_row_id"),
                         "kl-arm-length-stale-row-0000")
        self.assertEqual(intent.get("version"), _STALE_VERSION)
        self.assertEqual(self._journal_path.read_bytes(), journal_before)
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before)
        self.assertEqual(self._pointer_rows(), pointer_before)

    # -- C3 ------------------------------------------------------------------

    def test_old_receipt_and_unrelated_queue_untouched(self):
        # Refusal branch (stale advance stake, version 7).
        first = self._capture(_KEY, "alpha body")
        self._capture(_KEY, "alpha two body")
        self._seed_stake(_KEY, "beta body", "kl-arm-length-stale-row-0000",
                         _STALE_VERSION, state="cas_stale")
        # The designed-to-quarantine capture (exit 1, cas_stale envelope).
        self._capture_quarantined(_KEY, "beta body")
        other_dg = _digest("unrelated queued body")
        queue_entry = {
            "namespace": _NAMESPACE, "target": _TARGET, "key": "guides/other",
            "version": 1, "locator": _locator("guides/other", other_dg),
            "fullhash": [other_dg[i * 8:(i + 1) * 8] for i in range(8)],
            "summary": "knowledge note guides/other",
            "date": "2026-01-01T00:00:00Z", "expected_row_id": None,
            "op_id": "kl-unrelated-queued-op",
            "enqueued_at": "2026-01-01T00:00:00Z", "attempts": 0,
            "last_error": None, "status": "queued",
        }
        old_receipt = {
            "op_id": "kl-old-receipt-op", "target": _TARGET,
            "key": _KEY, "status": "committed", "link_status": "advanced",
            "version": 1, "row_id": first["pointer"]["row_id"],
            "error": None, "received_at": "2026-01-01T00:00:00Z",
        }
        _key_refs = ("knowledge:%s:%s" % (_TARGET, _KEY),
                     "knowledge:%s:guides/deploy2" % _TARGET)
        unrelated_before = [e for e in self._journal()["entries"]
                            if e["ref"] not in _key_refs]
        delivery = self._delivery()
        delivery["queue"].append(queue_entry)
        delivery["receipts"][old_receipt["op_id"]] = old_receipt
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")

        refusal = self._retry()
        self.assertEqual(
            refusal.returncode, 1,
            "the stale-stake refusal branch must exit 1 (stdout=%r "
            "stderr=%r)" % (refusal.stdout, refusal.stderr[-300:]))
        # (r8 3c) The old-receipt EXACT full-dict equality runs
        # IMMEDIATELY after the refusal, before any other action.
        self.assertEqual(
            self._delivery()["receipts"].get("kl-old-receipt-op"),
            old_receipt,
            "the old receipt stays EXACTLY byte-identical (full dict "
            "equality) immediately after the REFUSAL branch")

        # Success branch on a second identity (initial-create fixture).
        fx = self._fixture_initial_create_quarantine(key="guides/deploy2",
                                                     text="beta two body")
        self._tombstone_competitor(fx["competitor_row"])
        op_ids_before_success = self._op_ids()
        ok = self._retry("guides/deploy2")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        new_ids = {i for i in self._op_ids() - op_ids_before_success if i}
        self.assertTrue(new_ids, "the success retry minted a fresh op id")
        new_id = sorted(new_ids)[0]
        self.assertNotEqual(new_id, "kl-old-receipt-op",
                            "the new op id differs from the old receipt's")

        after = self._delivery()
        kept = [e for e in after["queue"]
                if e.get("op_id") == "kl-unrelated-queued-op"]
        self.assertEqual(kept, [queue_entry],
                         "the unrelated queued op stays byte-identical")
        # The old receipt compared EXACTLY (full dict equality) across
        # both the refusal and the success retry.
        self.assertEqual(after["receipts"].get("kl-old-receipt-op"),
                         old_receipt,
                         "the old receipt stays EXACTLY byte-identical "
                         "(full dict equality)")
        # Op-id linkage: BOTH the old receipt's op id and the retry's new
        # op id are reported by the status surface (local receipts are
        # keyed by op id in delivery.json).
        self.assertIn(new_id, after["receipts"],
                      "the retry's new op id is recorded as a receipt "
                      "(keyed by op id in delivery.json)")
        unrelated_after = [e for e in self._journal()["entries"]
                           if e["ref"] not in _key_refs]
        self.assertEqual(unrelated_after, unrelated_before,
                         "every unrelated journal entry stays byte-identical")
        status = self._cli("knowledge", "status", "--json")
        ids = [r.get("op_id") for r in
               json.loads(status.stdout)["delivery"]["receipts"]]
        self.assertIn("kl-old-receipt-op", ids,
                      "the old receipt stays READABLE through status")
        self.assertIn(new_id, ids,
                      "the retry's new op id is ALSO reported by status "
                      "alongside the old receipt (op-id linkage)")

    # -- C4 ------------------------------------------------------------------

    def test_no_invocation_no_effect(self):
        self._fixture_initial_create_quarantine()
        journal_before = self._journal_path.read_bytes()
        delivery_before = self._delivery_path.read_bytes()

        for args in (
            ("knowledge", "status", "--target", _TARGET, "--key",
             _KEY, "--namespace", _NAMESPACE, "--json"),
            ("knowledge", "read", "--target", _TARGET, "--key",
             _KEY, "--namespace", _NAMESPACE, "--json"),
            ("knowledge", "check", "--target", _TARGET, "--key",
             _KEY, "--namespace", _NAMESPACE, "--json"),
            ("knowledge", "flush", "--json"),
        ):
            r = self._cli(*args)
            self.assertIn(r.returncode, (0, 1, 3))
            self.assertEqual(self._journal_path.read_bytes(), journal_before,
                             "%s must not touch the journal" % args[1])
            self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                             "%s must not touch the delivery sidecar"
                             % args[1])

        quarantined_before = sorted(self._entries_for(_KEY),
                                    key=lambda e: e["seq"])
        intent_before = self._intent(_KEY)
        self._capture("guides/different", "different key body")
        self.assertEqual(sorted(self._entries_for(_KEY),
                                key=lambda e: e["seq"]),
                         quarantined_before,
                         "no new entry and no mutation for the quarantined "
                         "identity")
        self.assertEqual(self._intent(_KEY), intent_before)

    # -- supplementary negatives ----------------------------------------------

    def test_retry_selects_latest_quarantined_version(self):
        # beta v1 quarantined against the competing first-create.
        fx1 = self._fixture_initial_create_quarantine(text="beta one body")
        # beta v2: a second crashed initial stake -> a SECOND quarantined
        # journal entry (the journal keeps both; the intent carries v2).
        fx2 = self._fixture_initial_create_quarantine(text="beta two body")
        self.assertNotEqual(fx1["seq"], fx2["seq"])

        self._tombstone_competitor(fx2["competitor_row"])
        result = self._retry()
        self.assertEqual(result.returncode, 0,
                         "stdout=%r stderr=%r" % (result.stdout[-300:],
                                                  result.stderr[-300:]))
        # The LATEST quarantined version's digest lands.
        status = self._cli("knowledge", "status", "--target", _TARGET,
                           "--key", _KEY, "--namespace", _NAMESPACE, "--json")
        pointer = json.loads(status.stdout)["pointers"][0]["pointer"]
        self.assertEqual(pointer["full_sha256"], fx2["digest"],
                         "retry must select the LATEST cas_stale entry")
        # BOTH old entries survive verbatim.
        entries = self._entries_for(_KEY)
        for fx in (fx1, fx2):
            same = [e for e in entries if e["seq"] == fx["seq"]]
            self.assertEqual(same, [json.loads(json.dumps(fx["entry"]))])

    def test_retry_same_version_different_digest_live_is_cas_stale(self):
        """FB-002 coverage gap (critic mutant M2): a live pointer at the
        SAME version carrying a DIFFERENT digest is ANOTHER WRITER'S row —
        never this retry's landed crash window.  The FB-002
        post-link-crash reconciliation must require the EXACT digest (and
        locator), not the version alone: the retry refuses typed
        ``cas_stale``, mutates NOTHING, and the live digest stays the
        other writer's.  (A version-only ``_landed_match`` mutant would
        wrongly reconcile this as applied/noop — this test is its
        killer.)"""
        fx = self._fixture_initial_create_quarantine()
        # Tombstone the competing first-create: no live pointer remains,
        # so beta's INITIAL-CREATE stake (None, 1, digest A) is CURRENT.
        self._tombstone_competitor(fx["competitor_row"])
        self.assertIsNone(
            self._live_pointer(),
            "PRE-CONDITION: no live pointer after the tombstone")

        # Another writer links a DIFFERENT digest at the SAME version 1
        # directly on the shared store (this host's sidecars untouched:
        # the delivery intent still carries beta's (None, 1, digest A)
        # cas_stale stake verbatim).
        digest_b = _digest("gamma body")
        self._link_other_writer(version=1, digest=digest_b,
                                summary="another writer v1")
        live = self._live_pointer()
        self.assertIsNotNone(live, "the other writer's row is live")

        journal_before = self._journal_path.read_bytes()
        delivery_before = self._delivery_path.read_bytes()
        result = self._retry()
        self.assertEqual(
            result.returncode, 1,
            "a same-version DIFFERENT-digest live pointer must refuse "
            "the retry typed; got %d stdout=%r" % (result.returncode,
                                                   result.stdout))
        body = json.loads(result.stdout)
        self.assertIs(body.get("ok"), False)
        self.assertEqual(
            (body.get("pointer") or {}).get("status"), "cas_stale",
            "the live row is another writer's (version 1, digest B != A): "
            "the honest cas_stale refusal — the crash-window "
            "reconciliation may not match on the version alone: %r"
            % (body,))

        # Refusals mutate NOTHING: both sidecars byte-identical.
        self.assertEqual(self._journal_path.read_bytes(), journal_before,
                         "the refused retry mutates no journal")
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused retry mutates no delivery sidecar")

        # The LIVE pointer is untouched: still the other writer's row at
        # version 1 with digest B.
        status = self._cli("knowledge", "status", "--target", _TARGET,
                           "--key", _KEY, "--namespace", _NAMESPACE,
                           "--json")
        pointer = json.loads(status.stdout)["pointers"][0]["pointer"]
        self.assertEqual(pointer["version"], 1,
                         "the live version is untouched: %r" % (pointer,))
        self.assertEqual(
            pointer["full_sha256"], digest_b,
            "the live digest remains the OTHER WRITER'S B (the refused "
            "retry never supersedes it): %r" % (pointer,))

    def test_retry_on_malformed_recovery_state_refuses_typed(self):
        self._fixture_initial_create_quarantine()
        # Malformed journal: corrupt JSON -> a typed refusal, no traceback.
        self._journal_path.write_bytes(b"{not json")
        r = self._retry()
        self.assertEqual(r.returncode, 1,
                         "malformed recovery state must be a typed refusal "
                         "(exit 1), got %d stdout=%r" % (r.returncode,
                                                         r.stdout))
        self.assertFalse("Traceback" in r.stderr)
        body = json.loads(r.stdout) if r.stdout.strip() else {}
        self.assertIs(body.get("ok"), False)

        # Malformed intent: the stake record without its digest.
        self._journal_path.write_text(json.dumps(
            {"version": 1, "entries": [{
                "seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": _locator(_KEY, "0" * 64), "digest": "0" * 64,
                "state": "quarantined",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE}]}, indent=1), encoding="utf-8")
        self._seed_stake(_KEY, "beta body", None, 1, state="cas_stale")
        delivery = self._delivery()
        delivery["intents"]["%s|knowledge:%s:%s" % (_NAMESPACE, _TARGET,
                                                    _KEY)].pop("digest")
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")
        r2 = self._retry()
        self.assertEqual(r2.returncode, 1,
                         "a malformed intent must be a typed refusal; got "
                         "%d stdout=%r" % (r2.returncode, r2.stdout))
        self.assertFalse("Traceback" in r2.stderr)

    def test_concurrent_retries_exactly_one_succeeds(self):
        fx = self._fixture_initial_create_quarantine()
        self._tombstone_competitor(fx["competitor_row"])
        # Two simultaneous retry processes (both started before either
        # can finish; the journal byte-lock serializes them).
        proc1 = self._retry_popen()
        proc2 = self._retry_popen()
        out1 = proc1.communicate(timeout=180)
        out2 = proc2.communicate(timeout=180)
        codes = sorted([proc1.returncode, proc2.returncode])
        # FB-002: exactly one retry LINKS the pointer; the loser's stale
        # CAS either reconciles against the landed pointer (both exit 0 —
        # ``applied`` + ``noop``) OR, if the winner's journal mark landed
        # first, receives the TYPED refusal "already applied" (exit 1).
        # Both orderings are correct: no double link, no crash, no mutation
        # by the loser. Pin the invariant, not the scheduling order.
        self.assertIn(codes, ([0, 0], [0, 1]),
                      "one winner plus a reconciled noop or a typed "
                      "'already applied' refusal; codes=%r out1=%r out2=%r"
                      % (codes, out1[0][-300:], out2[0][-300:]))
        results = []
        for proc, out in ((proc1, out1), (proc2, out2)):
            body = json.loads(out[0]) if out[0].strip() else None
            self.assertIsInstance(body, dict, "each retry returns a JSON "
                                  "envelope: %r" % (out,))
            self.assertNotIn("Traceback", out[1])
            results.append((proc.returncode, body))
        ok_bodies = [b for rc, b in results if rc == 0 and b.get("ok") is True]
        refused = [b for rc, b in results if rc == 1 and b.get("ok") is False]
        self.assertEqual(len(ok_bodies) + len(refused), 2, results)
        if codes == [0, 0]:
            self.assertEqual(len(ok_bodies), 2, results)
            statuses = sorted((b.get("pointer") or {}).get("status")
                              for b in ok_bodies)
            self.assertEqual(statuses, ["applied", "noop"],
                             "one winner links (applied), the loser "
                             "completes as the reconciled noop: %r" % (results,))
            reconciled = [b for b in ok_bodies
                          if (b.get("pointer") or {}).get(
                              "reconciled_after_cas_stale") is True]
            self.assertEqual(len(reconciled), 1,
                             "exactly one reconciled completion: %r" % (results,))
        else:
            self.assertEqual(len(ok_bodies), 1, results)
            self.assertEqual(len(refused), 1, results)
            self.assertIn("already applied", str(refused[0].get("error") or ""))
            self.assertIn((ok_bodies[0].get("pointer") or {}).get("status"),
                          ("applied", "noop"))
        # In both orderings the winner left exactly one live pointer row.
        self.assertEqual(len(self._pointer_rows()), 1)

    def test_crash_partial_failure_is_re_retryable(self):
        fx = self._fixture_initial_create_quarantine()
        self._tombstone_competitor(fx["competitor_row"])
        # A crashed retry's partial state, seeded DIRECTLY through the
        # documented journal format (r8): record_intent() with the SAME
        # digest would DEDUPE onto the existing quarantined entry (the
        # r7 probe finding — the partial was never created), so the
        # crashed residue is a HIGHER-SEQ doc_pending entry for the same
        # digest, head-shaped where the new fields apply.
        obj = json.loads(self._journal_path.read_text(encoding="utf-8"))
        seq = max(int(e["seq"]) for e in obj["entries"]) + 1
        obj["entries"].append({
            "seq": seq, "target_id": _TARGET, "logical_key": _KEY,
            "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
            "locator": fx["locator"], "digest": fx["digest"],
            "state": "doc_pending",
            "updated_at": "2026-01-03T00:00:00.000+00:00",
            "namespace": _NAMESPACE,
            "op_id": "kl-crashed-partial", "retry_ref": fx["seq"],
            "summary": _EXPLICIT_SUMMARY,
            "staked_at": "2026-01-02T03:04:05Z",
        })
        self._journal_path.write_text(json.dumps(obj, indent=1),
                                      encoding="utf-8")
        # PRE-ASSERT the partial exists (r8): state doc_pending, seq >
        # original seq, for the same digest — BEFORE the retry.
        partial = [e for e in self._entries_for(_KEY)
                   if e["seq"] == seq]
        self.assertEqual(len(partial), 1,
                         "the crashed partial entry was created")
        self.assertEqual(partial[0]["state"], "doc_pending")
        self.assertGreater(partial[0]["seq"], fx["seq"])
        self.assertEqual(partial[0]["digest"], fx["digest"])
        self.assertIsNone(self._live_pointer(),
                          "the crashed retry left the pointer un-advanced")

        result = self._retry()
        self.assertEqual(result.returncode, 0,
                         "a fresh retry must re-select and complete the "
                         "crashed attempt; stdout=%r" % (result.stdout,))
        rows = self._pointer_rows()
        self.assertEqual(len(rows), 1)
        status = self._cli("knowledge", "status", "--target", _TARGET,
                           "--key", _KEY, "--namespace", _NAMESPACE, "--json")
        pointer = json.loads(status.stdout)["pointers"][0]["pointer"]
        self.assertEqual(pointer["full_sha256"], fx["digest"])
        # CRASH DISPOSITION (r7 corrected invariant): after the
        # completing retry there is (1) the ORIGINAL preserved entry
        # (verbatim, C1's preservation law), (2) EXACTLY ONE terminal
        # RETRY entry AFTER it (seq > original seq), and (3) NO entry
        # for the digest left in a non-terminal state (the crashed
        # partial is superseded).
        digest_entries = [e for e in self._entries_for(_KEY)
                          if e["digest"] == fx["digest"]]
        original_kept = [e for e in digest_entries if e["seq"] == fx["seq"]]
        self.assertEqual(original_kept, [json.loads(json.dumps(fx["entry"]))],
                         "(1) the ORIGINAL quarantined entry is preserved "
                         "verbatim")
        terminal_retries = [e for e in digest_entries
                            if e["seq"] > fx["seq"]
                            and e["state"] in ("applied", "quarantined")]
        self.assertEqual(
            len(terminal_retries), 1,
            "(2) EXACTLY ONE terminal RETRY entry after the original "
            "(seq > %d): %r" % (fx["seq"], digest_entries))
        active = [e for e in digest_entries
                  if e["state"] not in ("applied", "quarantined")]
        self.assertEqual(
            active, [],
            "(3) NO remaining ACTIVE/partial entry for the digest (the "
            "crashed partial is superseded): %r" % (active,))

    def test_post_link_crash_completes_on_re_retry(self):
        """FB-002: a retry that landed the pointer but crashed BEFORE its
        verdict reached the journal/intent leaves the sidecars saying
        'quarantined / cas_stale' while the pointer is LIVE.  The re-run's
        CAS refuses — but the authoritative pointer EXACTLY matches the
        staked attempt, so the re-retry must COMPLETE (noop +
        reconciled_after_cas_stale), never wedge on cas_stale."""
        fx = self._fixture_initial_create_quarantine()
        self._tombstone_competitor(fx["competitor_row"])
        journal_before = self._journal_path.read_bytes()
        delivery_before = self._delivery_path.read_bytes()

        # The first retry SUCCEEDS: the pointer lands...
        first = self._retry()
        self.assertEqual(first.returncode, 0,
                         first.stdout + first.stderr)
        self.assertEqual(len(self._pointer_rows()), 1)

        # ...then "crashes" after the SQLite commit but before the
        # verdict: restore the PRE-RETRY sidecar bytes; the pointer
        # stays landed.
        self._journal_path.write_bytes(journal_before)
        self._delivery_path.write_bytes(delivery_before)
        self.assertEqual(len(self._pointer_rows()), 1,
                         "the pointer stays landed across the restore")

        # The re-run re-selects the quarantined original; its (None, 1)
        # stake is now spent — the CAS refuses, the live pointer matches
        # EXACTLY, and the retry must COMPLETE: exit 0, completed,
        # pointer.status noop, reconciled_after_cas_stale true.
        second = self._retry()
        self.assertEqual(
            second.returncode, 0,
            "the post-link-crash re-retry must complete, not refuse; "
            "stdout=%r stderr=%r" % (second.stdout, second.stderr[-300:]))
        body = json.loads(second.stdout)
        self.assertIs(body.get("ok"), True)
        self.assertIs(body.get("completed"), True)
        pointer = body.get("pointer") or {}
        self.assertEqual(pointer.get("status"), "noop",
                         "the reconciled pointer reports noop: %r" % (body,))
        self.assertIs(pointer.get("reconciled_after_cas_stale"), True,
                      "the reconciliation flag is reported: %r" % (body,))
        self.assertTrue(pointer.get("row_id"))

        # EXACTLY ONE fresh terminal retry entry above the original seq
        # (the original preserved verbatim; no ACTIVE residue).
        digest_entries = [e for e in self._entries_for(_KEY)
                          if e["digest"] == fx["digest"]]
        original_kept = [e for e in digest_entries
                         if e["seq"] == fx["seq"]]
        self.assertEqual(original_kept,
                         [json.loads(json.dumps(fx["entry"]))],
                         "the ORIGINAL quarantined entry stays verbatim")
        terminal_retries = [e for e in digest_entries
                            if e["seq"] > fx["seq"]
                            and e["state"] in ("applied", "quarantined")]
        self.assertEqual(
            len(terminal_retries), 1,
            "EXACTLY ONE fresh terminal retry entry after the re-retry: "
            "%r" % (digest_entries,))
        active = [e for e in digest_entries
                  if e["state"] not in ("applied", "quarantined")]
        self.assertEqual(active, [])

        # The recovery records the LANDED row (intent applied + receipt).
        intent = self._intent(_KEY)
        self.assertEqual(intent.get("state"), "applied")
        self.assertTrue(intent.get("row_id"))
        receipts = self._delivery()["receipts"]
        self.assertIn(body.get("op_id"), receipts)
        self.assertIs(
            receipts[body["op_id"]].get("reconciled_after_cas_stale"), True)

    def test_target_tamper_refuses_without_link(self):
        fx = self._fixture_initial_create_quarantine()
        self._tombstone_competitor(fx["competitor_row"])
        # Tamper with the staked bytes on the target.
        target_file = self.vault / fx["locator"]
        self.assertTrue(target_file.is_file())
        target_file.write_bytes(b"tampered replacement bytes\n")

        result = self._retry()
        self.assertEqual(result.returncode, 1,
                         "tampered staked bytes must refuse typed; got %d "
                         "stdout=%r" % (result.returncode, result.stdout))
        body = json.loads(result.stdout) if result.stdout.strip() else {}
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", ""))
        self.assertTrue("digest" in error.lower() or "readback" in error.lower()
                        or "tamper" in error.lower(),
                        "the refusal must name the readback/digest mismatch: "
                        "%r" % (error,))
        self.assertEqual(self._pointer_rows(), [],
                         "no pointer row may be linked against tampered "
                         "bytes")
        self.assertEqual(target_file.read_bytes(),
                         b"tampered replacement bytes\n",
                         "the tampered bytes are never overwritten by the "
                         "refusal")

    def test_retry_gated_by_zmem_capture_kill_switch(self):
        fresh = Path(tempfile.mkdtemp(prefix="zmem-knretry-gate-")).resolve()
        self.addCleanup(shutil.rmtree, str(fresh), True)
        (fresh / "vault").mkdir()
        env = {**os.environ}
        env["ZMEM_STORE"] = str(fresh / "store.sqlite").replace("\\", "/")
        env["ZMEM_DATA"] = str(fresh).replace("\\", "/")
        env["ZMEM_MODELS_DIR"] = str(fresh / "no-models")
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        env.pop("ZMEM_REMOTE_URL", None)
        env["ZMEM_CAPTURE"] = "0"
        r = self._cli("stats", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        (fresh / "config.json").write_text(json.dumps({
            "knowledge": {"enabled": True, "targets": {
                _TARGET: {"kind": "markdown", "root": str(fresh / "vault")}}}
        }), encoding="utf-8")
        gated = self._cli("knowledge", "retry", "--target", _TARGET,
                          "--key", _KEY, "--namespace", _NAMESPACE, "--json",
                          env=env)
        self.assertNotEqual(gated.returncode, 0)
        self.assertIn("ZMEM_CAPTURE=0", gated.stdout + gated.stderr)
        self.assertFalse((fresh / "knowledge").exists(),
                         "the gate fires BEFORE any sidecar directory is "
                         "created")

    def test_retry_disabled_config_refuses(self):
        self._capture(_KEY, "alpha body")
        (self.tmp / "config.json").write_text("{}", encoding="utf-8")
        r = self._retry()
        self.assertNotEqual(r.returncode, 0)
        body = json.loads(r.stdout) if r.stdout.strip() else {}
        self.assertIs(body.get("ok"), False)
        self.assertIn("disabled", str(body.get("error", "")).lower())

    def test_second_retry_after_success_refuses(self):
        fx = self._fixture_initial_create_quarantine()
        self._tombstone_competitor(fx["competitor_row"])
        first = self._retry()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self._retry()
        self.assertEqual(second.returncode, 1,
                         "idempotence-by-state: stdout=%r" % (second.stdout,))
        body = json.loads(second.stdout)
        self.assertIs(body.get("ok"), False)
        self.assertIn("quarantin", str(body.get("error", "")).lower())

    def test_retry_without_quarantined_intent_refuses(self):
        self._capture(_KEY, "alpha body")
        r = self._retry()
        self.assertEqual(r.returncode, 1, r.stdout)
        body = json.loads(r.stdout) if r.stdout.strip() else {}
        self.assertIs(body.get("ok"), False)
        self.assertIn("quarantin", str(body.get("error", "")).lower())

    # -- r7 non-CAS negatives ---------------------------------------------------
    # STATES finding (reported): knowledge/journal.py STATES carries
    # exactly ONE quarantine state ("quarantined") with no cause taxonomy
    # — the CAUSE distinction lives in the delivery intent's state field
    # ("cas_stale" / "conflict" / "rejected" / ...).  The hub-rejection
    # leg below therefore drives the cause through the intent state (no
    # skip needed).

    def test_retry_on_operator_quarantine_refuses(self):
        """(i) OPERATOR quarantine (a non-CAS cause: the entry driven
        terminal through the journal's own mark() escape hatch, intent
        still plain "staked") -> typed refusal naming the cause."""
        alpha = self._capture(_KEY, "alpha body")
        seeded = self._seed_stake(_KEY, "beta body",
                                  alpha["pointer"]["row_id"], 2,
                                  state="staked")
        sys.path.insert(0, str(SCRIPTS_DIR))
        from knowledge.journal import KnowledgeJournal  # noqa: PLC0415
        journal = KnowledgeJournal(self._journal_path)
        journal.record_intent(
            target_id=_TARGET, logical_key=_KEY,
            locator=seeded["locator"], digest=seeded["digest"],
            namespace=_NAMESPACE)
        journal.mark(target_id=_TARGET, logical_key=_KEY,
                     state="quarantined", namespace=_NAMESPACE)
        journal_before = self._journal_path.read_bytes()
        r = self._retry()
        self.assertEqual(r.returncode, 1,
                         "an operator-quarantined intent must refuse; got "
                         "%d stdout=%r stderr=%r"
                         % (r.returncode, r.stdout, r.stderr[-200:]))
        body = json.loads(r.stdout) if r.stdout.strip() else {}
        self.assertIs(body.get("ok"), False)
        self.assertIn("quarantin", str(body.get("error", "")).lower())
        self.assertEqual(self._journal_path.read_bytes(), journal_before,
                         "the refusal mutates nothing")

    def test_retry_on_mismatched_intent_refuses(self):
        """(ii) MISMATCH: the journal entry exists (quarantined) but the
        delivery intent names a DIFFERENT digest -> a typed refusal, never
        a wrong-entry retry; the journal stays byte-identical."""
        self._fixture_initial_create_quarantine()
        journal_before = self._journal_path.read_bytes()
        intent = self._intent(_KEY)
        intent["digest"] = "f" * 64          # a foreign digest
        intent["locator"] = _locator(_KEY, "f" * 64)
        delivery = self._delivery()
        delivery["intents"]["%s|knowledge:%s:%s" % (_NAMESPACE, _TARGET,
                                                    _KEY)] = intent
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")
        r = self._retry()
        self.assertEqual(r.returncode, 1,
                         "a mismatched journal/intent pair must refuse; "
                         "got %d stdout=%r" % (r.returncode, r.stdout))
        body = json.loads(r.stdout) if r.stdout.strip() else {}
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", "")).lower()
        self.assertTrue("mismatch" in error or "digest" in error,
                        "the refusal must name the mismatch: %r" % (error,))
        self.assertEqual(self._journal_path.read_bytes(), journal_before)

    def test_retry_on_hub_rejection_quarantine_refuses(self):
        """(iii) HUB-REJECTION quarantine cause (journal quarantined +
        intent state "rejected" — the documented cause marker in the
        delivery sidecar's state vocabulary) -> typed refusal naming the
        cause; never retryable merely because it is terminal."""
        seeded = self._seed_stake(_KEY, "beta body", None, 1,
                                  state="rejected")
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [{
                "seq": 2, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": seeded["locator"], "digest": seeded["digest"],
                "state": "quarantined",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE,
            }]}, indent=1), encoding="utf-8")
        journal_before = self._journal_path.read_bytes()
        r = self._retry()
        self.assertEqual(r.returncode, 1,
                         "a hub-rejection quarantine (non-CAS cause) must "
                         "refuse; got %d stdout=%r" % (r.returncode,
                                                       r.stdout))
        body = json.loads(r.stdout) if r.stdout.strip() else {}
        self.assertIs(body.get("ok"), False)
        self.assertIn("quarantin", str(body.get("error", "")).lower())
        self.assertEqual(self._journal_path.read_bytes(), journal_before)

    # -- r6 supplementary: journal schema compatibility -----------------------

    def _head_shaped_entry(self, seq: int = 1) -> dict:
        """A HEAD-shaped journal entry: the nine base fields plus the
        FOUR r6/r7 fields (op_id / retry_ref / summary / staked_at), ALL
        non-null where the schema allows it (no vacuous None==None)."""
        return {
            "seq": seq, "target_id": _TARGET, "logical_key": _KEY,
            "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
            "locator": _locator(_KEY, "0" * 64), "digest": "0" * 64,
            "state": "doc_pending",
            "updated_at": "2026-01-01T00:00:00.000+00:00",
            "namespace": _NAMESPACE,
            "op_id": "kl-orig-%d" % seq, "retry_ref": 1,
            "summary": "fleet deploy runbook summary",
            "staked_at": "2026-01-02T03:04:05Z",
        }

    def test_journal_base_parser_accepts_head_shaped_entry(self):
        """(a) BASE-PARSER ACCEPTANCE (GREEN at base): a BASE journal
        parser loading a head-written entry (the three new fields +
        retry_ref present) does NOT error — old readers keep working;
        they simply drop the unknown fields (the disclosed mixed-version
        loss; old-READER acceptance, not old-WRITER preservation)."""
        self._capture(_KEY, "alpha body")
        sys.path.insert(0, str(SCRIPTS_DIR))
        from knowledge.journal import JournalError, KnowledgeJournal
        try:
            entry = KnowledgeJournal._entry_from_json(self._head_shaped_entry())
        except JournalError as exc:  # pragma: no cover - the green path
            self.fail("the BASE parser must accept a head-shaped entry "
                      "without JournalError: %s" % (exc,))
        self.assertEqual(entry.seq, 1)
        self.assertEqual(entry.state, "doc_pending")

    def test_journal_head_parser_round_trips_new_fields(self):
        """(b) HEAD-PARSER ROUND-TRIP (RED at base): ALL FOUR new fields
        (op_id / retry_ref / summary / staked_at — retry_ref and op_id
        NON-NULL in the fixture) survive load -> a REAL mark() state
        transition -> _retained -> load again.  At base the parser
        builds the dataclass from the nine named fields only AND mark()
        reconstructs entries from base fields, so the new fields are
        DROPPED — the exact gap the fix closes (the mark() leg is the
        r7 transition-preservation gap)."""
        self._capture(_KEY, "alpha body")
        sys.path.insert(0, str(SCRIPTS_DIR))
        from knowledge.journal import KnowledgeJournal
        journal = KnowledgeJournal(self._journal_path)
        head_entry = self._head_shaped_entry(seq=2)   # state doc_pending
        self.assertNotEqual(head_entry["retry_ref"], None)
        self.assertNotEqual(head_entry["op_id"], None)
        obj = json.loads(self._journal_path.read_text(encoding="utf-8"))
        obj["entries"].append(head_entry)
        self._journal_path.write_text(json.dumps(obj, indent=1),
                                      encoding="utf-8")

        # Expected values come from the FIXTURE DICT (the head-shaped
        # literal) — never from the parser under test (a base parser
        # yields None for absent fields and a None==None comparison
        # would pass vacuously; the r7 no-vacuous-None rule).
        expected_fields = {name: head_entry[name]
                           for name in ("op_id", "retry_ref", "summary",
                                        "staked_at")}

        # (i) a REAL mark() transition (doc_pending -> quarantined, a
        # documented transition) must PRESERVE the four fields — mark()
        # reconstructs the entry from base fields at base, the r7 gap.
        marked = journal.mark(target_id=_TARGET, logical_key=_KEY,
                              state="quarantined", namespace=_NAMESPACE)
        self.assertEqual(marked.state, "quarantined")
        after_mark = [e for e in journal.entries() if e.seq == 2][0]
        for name, expected in expected_fields.items():
            self.assertEqual(
                getattr(after_mark, name, None), expected,
                "%s survives the REAL mark() transition (doc_pending -> "
                "quarantined): got=%r expected=%r"
                % (name, getattr(after_mark, name, None), expected))

        # (ii) load -> _retained (the write-path serialization) -> load
        # again keeps all four fields losslessly.
        retained = KnowledgeJournal._retained(
            journal, [journal._entry_from_json(head_entry)])
        round_tripped = journal._entry_from_json(retained[0].__dict__)
        for name, expected in expected_fields.items():
            self.assertEqual(
                getattr(round_tripped, name, None), expected,
                "%s round-trips losslessly (load -> _retained -> load): "
                "got=%r expected=%r"
                % (name, getattr(round_tripped, name, None), expected))

    def test_journal_head_parser_accepts_legacy_entries(self):
        """(c) LEGACY ACCEPTANCE: the HEAD parser also accepts a
        legacy-shaped (fieldless) entry — old journals load unchanged
        (parsed via item.get() so absent fields default to None)."""
        self._capture(_KEY, "alpha body")
        sys.path.insert(0, str(SCRIPTS_DIR))
        from knowledge.journal import JournalError, KnowledgeJournal
        try:
            entry = KnowledgeJournal._entry_from_json({
                "seq": 2, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": _locator(_KEY, "1" * 64), "digest": "1" * 64,
                "state": "quarantined",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE,
            })
        except JournalError as exc:
            self.fail("a legacy-shaped (fieldless) entry must load "
                      "unchanged: %s" % (exc,))
        self.assertEqual(entry.state, "quarantined")

    def test_hook_path_never_auto_retries(self):
        """HOST/HOOK no-auto-retry: with a quarantined state present, a
        real hook entry path (hooks/zmem-reflect.sh, the session-entry
        reflect hook) NEVER invokes retry — the recovery sidecars stay
        byte-identical after the hook runs (fail-open hook errors are
        irrelevant to the pin: only the sidecar bytes matter)."""
        self._fixture_initial_create_quarantine()
        journal_before = self._journal_path.read_bytes()
        delivery_before = self._delivery_path.read_bytes()
        hook = REPO_ROOT / "hooks" / "zmem-reflect.sh"
        self.assertTrue(hook.is_file(), "the reflect hook must exist")
        env = {**self.env, "ZMEM_SESSION": "sess-hook-probe",
               "ZMEM_NAMESPACE": _NAMESPACE}
        proc = subprocess.run(
            ["bash", str(hook)],
            input=json.dumps({"session_id": "sess-hook-probe"}),
            capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=str(REPO_ROOT), timeout=120)
        # Whatever the hook's own outcome (fail-open), the knowledge
        # recovery sidecars must be byte-identical: no hook surface ever
        # takes a retry action.
        self.assertEqual(self._journal_path.read_bytes(), journal_before,
                         "the hook path must not touch the journal (no "
                         "auto-retry); hook rc=%d" % (proc.returncode,))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the hook path must not touch the delivery "
                         "sidecar (no auto-retry)")

    # -- remote legs -----------------------------------------------------------

    def test_remote_retry_reachable_hub_reenqueues_original_stake(self):
        hub = _FakeHubServer()
        self.addCleanup(hub.close)
        # Remote scratch: sidecars in the data dir; no local store.
        self._env(remote=True, remote_url=hub.url)
        seeded = self._seed_stake(_KEY, "beta body", None, 1,
                                  state="cas_stale")
        # beta's document is published at the vault (the staked bytes).
        target_file = self.vault / seeded["locator"]
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(_canonical("beta body"))
        # The journal carries the quarantined entry WITH its retry
        # provenance (the crashed writer's record — summary + staked_at
        # live on the JOURNAL record, r6).
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [{
                "seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": seeded["locator"], "digest": seeded["digest"],
                "state": "quarantined",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE,
                "summary": "fleet deploy runbook summary",
                "staked_at": "2026-01-02T03:04:05Z",
            }]}, indent=1), encoding="utf-8")
        # An old receipt + an unrelated queued op must survive.
        other_dg = _digest("unrelated queued body")
        queue_entry = {
            "namespace": _NAMESPACE, "target": _TARGET, "key": "guides/other",
            "version": 1, "locator": _locator("guides/other", other_dg),
            "fullhash": [other_dg[i * 8:(i + 1) * 8] for i in range(8)],
            "summary": "knowledge note guides/other",
            "date": "2026-01-01T00:00:00Z", "expected_row_id": None,
            "op_id": "kl-unrelated-queued-op",
            "enqueued_at": "2026-01-01T00:00:00Z", "attempts": 0,
            "last_error": None, "status": "queued",
        }
        old_receipt = {
            "op_id": "kl-old-receipt-op", "target": _TARGET, "key": _KEY,
            "status": "committed", "link_status": "advanced", "version": 1,
            "row_id": None, "error": None,
            "received_at": "2026-01-01T00:00:00Z",
        }
        delivery = self._delivery()
        delivery["queue"].append(queue_entry)
        delivery["receipts"][old_receipt["op_id"]] = old_receipt
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")

        result = self._retry()
        self.assertEqual(result.returncode, 0,
                         "remote retry against a capable hub must succeed; "
                         "stdout=%r stderr=%r" % (result.stdout[-400:],
                                                  result.stderr[-400:]))
        body = json.loads(result.stdout)
        self.assertIs(body.get("ok"), True)
        # QUEUED semantics (frozen r4/r5): the retry ENQUEUES a fresh op;
        # the flush lane applies LATER — nothing here asserts "applied",
        # and the hub received ZERO apply_ops calls during the retry
        # (capability reads hello/knowledge_pointer only).
        self.assertIs(body.get("queued"), True,
                      "the reachable-hub envelope reports queued=true "
                      "(not applied): %r" % (body,))
        self.assertEqual(
            _FakeMCPHub.tool_calls.count("apply_ops"), 0,
            "remote retry ENQUEUES only — ZERO apply_ops calls reached "
            "the hub during retry (apply happens at flush); calls=%r"
            % (_FakeMCPHub.tool_calls,))
        # Round-2 pin: the retry's OWN journal entry is pointer_queued
        # (never applied at retry time — the flush lane advances it), the
        # quarantined ORIGINAL stays preserved, and the reported op id is
        # the DURABLE queued identity (the queue entry carries it).
        entries = self._entries_for(_KEY)
        retry_entries = [e for e in entries
                         if e["seq"] > json.loads(
                             self._journal_path.read_text(encoding="utf-8")
                             )["entries"][0].get("seq", 0)
                         and e.get("retry_ref") is not None] \
            if entries else []
        queued_retry = [e for e in entries
                        if e.get("state") == "pointer_queued"
                        and e.get("retry_ref") is not None]
        self.assertTrue(queued_retry,
                        "the remote retry lands a pointer_queued journal "
                        "entry (flush advances it later): %r" % (entries,))
        self.assertEqual(queued_retry[0].get("op_id"), body.get("op_id"),
                         "the reported op id is the DURABLE queued "
                         "identity: %r vs %r"
                         % (queued_retry[0].get("op_id"), body.get("op_id")))
        quarantined = [e for e in entries
                       if e.get("state") == "quarantined"]
        self.assertTrue(quarantined,
                        "the quarantined ORIGINAL is preserved verbatim "
                        "by the remote retry")
        # Idempotent recovery (crash window): a SECOND retry re-selects
        # the quarantined entry; enqueue is idempotent by identity and
        # returns the SAME queued op id — re-invocation IS the recovery
        # path for a crash between enqueue and the journal write.
        again = self._retry()
        self.assertEqual(again.returncode, 0,
                         "a repeated remote retry re-selects and stays "
                         "queued (idempotent recovery): %r"
                         % (again.stdout[-200:],))
        again_body = json.loads(again.stdout)
        self.assertEqual(again_body.get("op_id"), body.get("op_id"),
                         "the repeated retry reports the SAME durable "
                         "queued op id: %r vs %r"
                         % (again_body.get("op_id"), body.get("op_id")))
        entries2 = self._entries_for(_KEY)
        self.assertEqual(
            len([e for e in entries2
                 if e.get("state") == "pointer_queued"
                 and e.get("retry_ref") is not None]), 1,
            "exactly ONE pointer_queued retry entry exists after the "
            "repeat (record_retry is idempotent on the retry identity)")

        self.assertTrue(
            set(_FakeMCPHub.tool_calls) <= {"hello", "knowledge_pointer"},
            "only capability/read calls are allowed during retry; "
            "calls=%r" % (_FakeMCPHub.tool_calls,))

        # The enqueued op carried the ORIGINAL stake VERBATIM (a fresh op
        # id, original expected_row_id + version + digest) and sits in
        # the queue with status "queued".
        after = self._delivery()
        ours = [e for e in after["queue"]
                if e.get("key") == _KEY and e.get("op_id")
                not in ("kl-unrelated-queued-op",)]
        self.assertEqual(len(ours), 1,
                         "exactly one freshly enqueued retry op: %r"
                         % (after["queue"],))
        entry = ours[0]
        fresh_id = entry["op_id"]
        self.assertTrue(fresh_id, "a fresh non-None op id")
        self.assertNotIn(fresh_id, ("kl-old-receipt-op",
                                    "kl-unrelated-queued-op"))
        self.assertEqual(entry.get("status"), "queued",
                         "the retry op is ENQUEUED (the flush lane applies "
                         "later): %r" % (entry,))
        self.assertEqual(entry.get("expected_row_id"), None,
                         "the ORIGINAL stake row id rides verbatim")
        self.assertEqual(entry.get("version"), 1,
                         "the ORIGINAL stake version rides verbatim")
        self.assertEqual(
            entry.get("fullhash"),
            [seeded["digest"][i * 8:(i + 1) * 8] for i in range(8)],
            "the ORIGINAL digest rides verbatim (chunked)")
        # The ORIGINAL summary/staked_at ride the enqueued op verbatim.
        self.assertEqual(
            entry.get("summary"), "fleet deploy runbook summary",
            "the ORIGINAL summary rides the enqueued op verbatim: %r"
            % (entry.get("summary"),))
        self.assertEqual(
            entry.get("staked_at"), "2026-01-02T03:04:05Z",
            "the ORIGINAL staked_at rides the enqueued op verbatim: %r"
            % (entry.get("staked_at"),))

        # The OLD RECEIPT dict is EXACTLY preserved (full equality) and
        # the unrelated queue entry survives untouched.
        self.assertEqual(after["receipts"].get("kl-old-receipt-op"),
                         old_receipt,
                         "the old receipt is EXACTLY preserved across the "
                         "remote retry (full dict equality)")
        kept = [e for e in after["queue"]
                if e.get("op_id") == "kl-unrelated-queued-op"]
        self.assertEqual(kept, [queue_entry])

    def test_remote_retry_pending_envelope_when_hub_unreachable(self):
        self._env(remote=True)   # unreachable loopback URL
        seeded = self._seed_stake(_KEY, "beta body", None, 1,
                                  state="cas_stale")
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [{
                "seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": seeded["locator"], "digest": seeded["digest"],
                "state": "quarantined",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE,
            }]}, indent=1), encoding="utf-8")
        delivery_before = self._delivery_path.read_bytes()

        r = self._retry()
        self.assertEqual(r.returncode, 3,
                         "remote retry with an unreachable hub answers the "
                         "pending envelope; got %d stdout=%r"
                         % (r.returncode, r.stdout))
        body = json.loads(r.stdout)
        self.assertIs(body.get("ok"), False)
        self.assertEqual((body.get("pointer") or {}).get("status"), "pending")
        self.assertTrue(str((body.get("pointer") or {}).get("reason", ""))
                        .startswith("hub-"))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "capability gate honored: NOTHING enqueued")

    # -- FB-017: the remote stale-CAS wedge -----------------------------------

    def test_remote_cas_stale_rejection_marks_intent_cas_stale(self):
        """A remote capture whose hub flush rejects with
        pointer_cas_stale (and the hub pointer read proves NOT landed)
        must record the intent at state ``cas_stale`` — the
        retry-recoverable cause — so ``knowledge retry`` can re-enqueue
        the original stake instead of wedging on an unrecoverable
        ``rejected``."""
        hub = _FakeHubServer(reject_error="pointer_cas_stale")
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        r = self._cli("knowledge", "capture", "--target", _TARGET,
                      "--key", _KEY, "--namespace", _NAMESPACE,
                      "--content", "beta body", "--summary",
                      _EXPLICIT_SUMMARY, "--json")
        self.assertEqual(
            r.returncode, 1,
            "the pointer-cas_stale-rejected capture exits 1: %s%s"
            % (r.stdout[-300:], r.stderr[-300:]))
        body = json.loads(r.stdout)
        self.assertEqual((body.get("pointer") or {}).get("status"),
                         "rejected")
        intent = self._intent(_KEY)
        self.assertIsNotNone(intent, "the capture wrote its intent")
        self.assertEqual(
            intent.get("state"), "cas_stale",
            "FB-017: a pointer_cas_stale rejection that did NOT "
            "reconcile as landed records the RETRYABLE intent state "
            "'cas_stale': %r" % (intent,))
        entries = self._entries_for(_KEY)
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["state"], "quarantined",
                         "the attempt's journal entry is terminal "
                         "quarantined: %r" % (entries[-1],))

        # The remote retry now SELECTS the quarantined cas_stale attempt
        # and ENQUEUES a fresh op carrying the ORIGINAL stake verbatim.
        rr = self._retry()
        self.assertEqual(
            rr.returncode, 0,
            "remote retry must recover the cas_stale quarantine: %s%s"
            % (rr.stdout[-300:], rr.stderr[-300:]))
        rbody = json.loads(rr.stdout)
        self.assertIs(rbody.get("ok"), True)
        self.assertIs(rbody.get("queued"), True)
        after = self._delivery()
        ours = [e for e in after["queue"] if e.get("key") == _KEY]
        self.assertEqual(len(ours), 1,
                         "exactly one freshly enqueued retry op: %r"
                         % (after["queue"],))
        entry = ours[0]
        self.assertEqual(entry.get("status"), "queued")
        self.assertEqual(entry.get("expected_row_id"), None,
                         "the ORIGINAL stake row id rides verbatim")
        self.assertEqual(entry.get("version"), 1,
                         "the ORIGINAL stake version rides verbatim")
        beta = _digest("beta body")
        self.assertEqual(
            entry.get("fullhash"),
            [beta[i * 8:(i + 1) * 8] for i in range(8)],
            "the ORIGINAL digest rides verbatim (chunked)")

    def test_flush_lane_cas_stale_rejection_marks_intent_cas_stale(self):
        """The FLUSH lane equivalent: a queued op rejected by the hub with
        pointer_cas_stale (not landed) drives the journal entry to
        quarantined and the intent to ``cas_stale`` — retry then
        recovers."""
        hub = _FakeHubServer(reject_error="pointer_cas_stale")
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        seeded = self._seed_stake(_KEY, "beta body", None, 1,
                                  state="queued")
        # The ACTIVE journal entry the flush will quarantine.
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [{
                "seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": seeded["locator"], "digest": seeded["digest"],
                "state": "pointer_queued",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE,
            }]}, indent=1), encoding="utf-8")
        # The in-flight queue entry carrying the same stake.
        delivery = self._delivery()
        delivery["queue"].append({
            "namespace": _NAMESPACE, "target": _TARGET, "key": _KEY,
            "version": 1, "locator": seeded["locator"],
            "fullhash": [seeded["digest"][i * 8:(i + 1) * 8]
                         for i in range(8)],
            "summary": _EXPLICIT_SUMMARY,
            "date": "2026-01-01T00:00:00Z", "expected_row_id": None,
            "op_id": "kl-flush-cas-stale-op",
            "enqueued_at": "2026-01-01T00:00:00Z", "attempts": 0,
            "last_error": None, "status": "queued",
        })
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 1,
                         "the rejected flush exits 1: %r" % (flushed.stdout,))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "cas_stale",
            "FB-017 flush lane: the pointer_cas_stale rejection records "
            "the RETRYABLE intent state: %r" % (intent,))
        self.assertEqual(intent.get("op_id"), "kl-flush-cas-stale-op")
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "quarantined",
                         "the flush quarantined the ACTIVE entry")
        receipts = self._delivery()["receipts"]
        self.assertEqual(
            receipts.get("kl-flush-cas-stale-op", {}).get("error"),
            "pointer_cas_stale")

        # And the retry recovers the flush-lane quarantine.
        rr = self._retry()
        self.assertEqual(rr.returncode, 0,
                         "retry recovers the flush-lane cas_stale "
                         "quarantine: %r" % (rr.stdout[-300:],))
        rbody = json.loads(rr.stdout)
        self.assertIs(rbody.get("queued"), True)

    def test_non_cas_rejection_of_enqueued_retry_settles_intent(self):
        """FB-017 rework: the in-flight retry op is ASSOCIATED with its
        intent (the remote enqueue re-points the intent's op_id, the
        symmetric close of the local branch), so a hub rejection of the
        RETRY op for a NON-CAS cause settles the intent to terminal
        ``rejected`` — the repeat retry refuses typed naming the cause and
        re-enqueues NOTHING, instead of leaving the original cas_stale
        stake selectable forever."""
        hub = _FakeHubServer(reject_error="pointer_cas_stale")
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        # Phase 1: a REAL capture quarantines cas_stale (the recoverable
        # cause) through the genuine hub-rejection path.
        r = self._cli("knowledge", "capture", "--target", _TARGET,
                      "--key", _KEY, "--namespace", _NAMESPACE,
                      "--content", "beta body", "--summary",
                      _EXPLICIT_SUMMARY, "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self._intent(_KEY).get("state"), "cas_stale")
        original_entry = self._entries_for(_KEY)[-1]
        self.assertEqual(original_entry["state"], "quarantined")

        # The remote retry ENQUEUES op A (the hub is never applied during
        # retry) and re-points the intent's op_id at the durable op.
        rr = self._retry()
        self.assertEqual(rr.returncode, 0,
                         "remote retry enqueues the recovery op: %s%s"
                         % (rr.stdout[-300:], rr.stderr[-300:]))
        rbody = json.loads(rr.stdout)
        self.assertIs(rbody.get("queued"), True)
        op_a = rbody.get("op_id")
        self.assertTrue(op_a, "the retry reports its durable op id")
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "cas_stale",
            "the intent stays cas_stale (still retryable) while op A is "
            "in flight: %r" % (intent,))
        self.assertEqual(
            intent.get("op_id"), op_a,
            "FB-017 rework: the enqueue re-points the intent's op_id at "
            "the durable retry op so the flush lane can settle THIS "
            "attempt's verdict on it: %r" % (intent,))

        # Phase 2: the hub now rejects op A for a NON-CAS cause; the
        # flush records the rejection and settles the intent.
        _FakeMCPHub.reject_error = "namespace_not_allowed"
        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 1,
                         "the non-CAS-rejected flush exits 1: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertIn(op_a, fbody.get("journal_quarantined") or [],
                      "the retry op's rejection drives its journal entry "
                      "to quarantined: %r" % (fbody,))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "rejected",
            "the NON-CAS rejection of the RETRY op settles the intent "
            "(the old cas_stale stake is no longer selectable): %r"
            % (intent,))
        self.assertEqual(intent.get("op_id"), op_a)
        self.assertEqual(
            (self._delivery()["receipts"].get(op_a) or {}).get("error"),
            "namespace_not_allowed",
            "the rejection receipt names the NON-CAS machine cause")
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "quarantined",
                         "the retry's pointer_queued entry is terminal "
                         "quarantined: %r" % (entries[-1],))
        # The ORIGINAL quarantined entry survives verbatim below it.
        self.assertEqual(
            [e for e in entries if e["seq"] == original_entry["seq"]],
            [original_entry], "full entry-dict equality for the preserved "
            "original (verbatim)")

        # The repeat retry refuses typed naming the cause and re-enqueues
        # NOTHING (both sidecars byte-identical).
        delivery_before = self._delivery_path.read_bytes()
        journal_before = self._journal_path.read_bytes()
        again = self._retry()
        self.assertEqual(
            again.returncode, 1,
            "a repeat retry after the non-CAS rejection of op A must "
            "refuse typed; got %d stdout=%r" % (again.returncode,
                                                again.stdout))
        body = json.loads(again.stdout)
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", ""))
        self.assertIn("rejected", error,
                      "the refusal names the NON-CAS quarantine cause: %r"
                      % (error,))
        self.assertIn("stale CAS", error,
                      "the refusal names the recoverable-cause rule: %r"
                      % (error,))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused repeat retry re-enqueues NOTHING")
        self.assertEqual(self._journal_path.read_bytes(), journal_before,
                         "the refused repeat retry mutates no journal")

    def test_restart_recovery_settles_non_cas_rejected_retry_receipt(self):
        """FB-017 rework, restart leg: a flush that persisted the RETRY
        op's rejected receipt and emptied the queue but crashed before the
        reconciliation leaves the retry's pointer_queued journal entry plus
        the cas_stale intent naming the op — the NEXT flush settles BOTH
        from the persisted receipt (journal terminal quarantined, intent
        ``rejected``), and a repeat retry refuses typed without
        re-enqueueing."""
        hub = _FakeHubServer()
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        seeded = self._seed_stake(_KEY, "beta body", None, 1,
                                  state="cas_stale")
        op_a = "kl-retry-restart-op"
        # The post-crash sidecar: the rejected receipt persisted, the
        # queue EMPTY, the intent still cas_stale but naming the
        # in-flight retry op (the enqueue-time re-pointing).
        delivery = self._delivery()
        delivery["intents"]["%s|%s" % (_NAMESPACE,
                                       "knowledge:%s:%s" % (_TARGET,
                                                            _KEY))]["op_id"] \
            = op_a
        delivery["receipts"][op_a] = {
            "op_id": op_a, "target": _TARGET, "key": _KEY,
            "status": "rejected", "link_status": "rejected",
            "error": "namespace_not_allowed", "row_id": None,
            # FB-017 round 2: the REAL receipt a flush persists for this
            # op carries the stake's row identity (the seeded stake is
            # the C1 INITIAL create: None) — the seeded receipt models
            # that shape; recovery REQUIRES it.
            "version": 1, "expected_row_id": None,
            "received_at": "2026-01-03T00:00:00Z",
        }
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")
        # The journal: the preserved quarantined ORIGINAL plus the
        # retry's pointer_queued entry (retry_ref -> the original).
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [
                {"seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                 "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                 "locator": seeded["locator"], "digest": seeded["digest"],
                 "state": "quarantined",
                 "updated_at": "2026-01-01T00:00:00.000+00:00",
                 "namespace": _NAMESPACE,
                 "summary": _EXPLICIT_SUMMARY,
                 "staked_at": "2026-01-02T03:04:05Z"},
                {"seq": 2, "target_id": _TARGET, "logical_key": _KEY,
                 "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                 "locator": seeded["locator"], "digest": seeded["digest"],
                 "state": "pointer_queued",
                 "updated_at": "2026-01-03T00:00:00.000+00:00",
                 "namespace": _NAMESPACE, "op_id": op_a, "retry_ref": 1,
                 "summary": _EXPLICIT_SUMMARY,
                 "staked_at": "2026-01-02T03:04:05Z"},
            ]}, indent=1), encoding="utf-8")

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 1,
                         "the settled-rejection flush exits 1: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the restart leg reconciles the persisted "
                         "rejected retry receipt: %r" % (fbody,))
        self.assertIn(op_a, fbody.get("journal_quarantined") or [],
                      "the retry's journal entry is driven to quarantined "
                      "off the persisted receipt: %r" % (fbody,))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "rejected",
            "the restart lane settles the intent off the persisted "
            "NON-CAS rejected retry receipt (the stake is no longer "
            "selectable): %r" % (intent,))
        self.assertEqual(intent.get("op_id"), op_a)
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "quarantined",
                         "the retry's journal entry is terminal "
                         "quarantined: %r" % (entries[-1],))
        self.assertEqual(
            [e for e in entries if e["seq"] == 1 and e["state"]
             == "quarantined"][0].get("digest"), seeded["digest"],
            "the ORIGINAL quarantined entry survives verbatim below it")

        # The repeat retry refuses typed (the cause is not a stale CAS)
        # and re-enqueues NOTHING.
        delivery_before = self._delivery_path.read_bytes()
        again = self._retry()
        self.assertEqual(
            again.returncode, 1,
            "the repeat retry must refuse typed after the "
            "restart-settled rejection; got %d stdout=%r"
            % (again.returncode, again.stdout))
        body = json.loads(again.stdout)
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", ""))
        self.assertIn("rejected", error,
                      "the refusal names the quarantine cause: %r"
                      % (error,))
        self.assertIn("stale CAS", error,
                      "the refusal names the recoverable-cause rule: %r"
                      % (error,))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused repeat retry re-enqueues NOTHING")

    # -- FB-017 stage B rework: BOTH crash windows -----------------------------

    def _seed_crashed_retry_flush(self, *, retry_op: str, intent_op: str,
                                  receipt: dict,
                                  retry_state: str = "pointer_queued",
                                  stake_version: int = 1,
                                  stake_expected_row_id=None) -> str:
        """The post-crash sidecars for a RETRY op's restart recovery: the
        retry's receipt persisted by a flush that died BEFORE its
        reconciliation, the queue EMPTY, the journal carrying the
        preserved quarantined ORIGINAL plus the retry's entry (retry_ref
        -> the original) seeded in ``retry_state`` — ``pointer_queued``
        (crash before the journal's applied mark), ``applied`` (the
        FB-017 FINAL window: crash between the applied mark and the stake
        settlement) or ``quarantined`` (the FINAL rejection-side window:
        crash between the journal quarantine and the stake settlement) —
        and the intent still ``cas_stale`` naming
        ``intent_op`` — either the durable retry op (the enqueue-time
        re-pointing ran) or the ORIGINAL capture's op (the crash window
        between record_retry and the re-point).  ``stake_version`` /
        ``stake_expected_row_id`` control the seeded stake (default: the
        C1 INITIAL stake (None, 1)).
        Returns the staked digest."""
        hub = _FakeHubServer()
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        seeded = self._seed_stake(_KEY, "beta body", stake_expected_row_id,
                                  stake_version, state="cas_stale")
        # FB-017 rework round 2: the REAL receipt a flush persists for a
        # queued op carries the stake's row identity alongside its
        # version (delivery persists ``expected_row_id`` off the queue
        # entry).  The seeded receipt models that real shape — the key is
        # filled from the seeded stake unless the caller pins it
        # explicitly — because restart recovery now REQUIRES it (a
        # receipt lacking the key binds nothing).
        receipt.setdefault("expected_row_id", stake_expected_row_id)
        delivery = self._delivery()
        delivery["intents"]["%s|%s" % (_NAMESPACE,
                                       "knowledge:%s:%s" % (_TARGET,
                                                            _KEY))]["op_id"] \
            = intent_op
        delivery["receipts"][retry_op] = receipt
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [
                {"seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                 "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                 "locator": seeded["locator"], "digest": seeded["digest"],
                 "state": "quarantined",
                 "updated_at": "2026-01-01T00:00:00.000+00:00",
                 "namespace": _NAMESPACE,
                 "summary": _EXPLICIT_SUMMARY,
                 "staked_at": "2026-01-02T03:04:05Z"},
                {"seq": 2, "target_id": _TARGET, "logical_key": _KEY,
                 "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                 "locator": seeded["locator"], "digest": seeded["digest"],
                 "state": retry_state,
                 "updated_at": "2026-01-03T00:00:00.000+00:00",
                 "namespace": _NAMESPACE, "op_id": retry_op,
                 "retry_ref": 1,
                 "summary": _EXPLICIT_SUMMARY,
                 "staked_at": "2026-01-02T03:04:05Z"},
            ]}, indent=1), encoding="utf-8")
        return seeded["digest"]

    def test_restart_recovery_committed_receipt_settles_repointed_intent(self):
        """FB-017 stage B, window A (committed receipt): a flush that
        persisted the RETRY op's COMMITTED receipt and crashed before the
        reconciliation used to advance the retry's journal entry to
        applied while leaving the cas_stale intent — here naming the
        durable retry op (the enqueue-time re-pointing ran) — selectable
        forever.  The next flush settles BOTH: journal applied AND intent
        ``applied`` with the receipt's authoritative row_id; the stake is
        spent and a repeat retry refuses typed."""
        op_a = "kl-retry-commit-op"
        digest = self._seed_crashed_retry_flush(
            retry_op=op_a, intent_op=op_a,
            receipt={"op_id": op_a, "target": _TARGET, "key": _KEY,
                     "status": "committed", "link_status": None,
                     "version": 1, "row_id": "row-" + op_a,
                     "error": None,
                     "received_at": "2026-01-03T00:00:00Z"})

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 0,
                         "the committed-receipt recovery flush exits 0: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertIs(fbody.get("ok"), True,
                      "nothing pending, rejected or quarantined: %r"
                      % (fbody,))
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the restart leg reconciles the persisted "
                         "committed retry receipt: %r" % (fbody,))
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "applied",
                         "the retry's journal entry advances to applied "
                         "off the committed receipt: %r" % (entries[-1],))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "applied",
            "window A: the committed receipt settles the cas_stale intent "
            "naming the durable retry op (the stale stake is no longer "
            "selectable): %r" % (intent,))
        self.assertEqual(intent.get("op_id"), op_a,
                         "the settled intent names the durable retry op")
        self.assertEqual(intent.get("row_id"), "row-" + op_a,
                         "the settled intent carries the receipt's "
                         "authoritative row_id: %r" % (intent,))
        self.assertEqual(intent.get("digest"), digest,
                         "the settled intent keeps the staked digest")
        self.assertEqual(
            [e for e in entries if e["seq"] == 1][0]["state"],
            "quarantined",
            "the ORIGINAL quarantined entry survives verbatim below it")

        # The stake is spent: a repeat retry refuses typed and re-enqueues
        # NOTHING (the latest journal entry is applied).
        delivery_before = self._delivery_path.read_bytes()
        again = self._retry()
        self.assertEqual(
            again.returncode, 1,
            "the repeat retry must refuse typed after the committed-"
            "receipt settlement; got %d stdout=%r"
            % (again.returncode, again.stdout))
        body = json.loads(again.stdout)
        self.assertIs(body.get("ok"), False)
        self.assertIn("already applied", str(body.get("error", "")),
                      "the refusal names the applied state: %r"
                      % (body.get("error"),))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused repeat retry re-enqueues NOTHING")

    def test_restart_recovery_committed_receipt_settles_pre_repoint_intent(self):
        """FB-017 stage B, window B (crash BEFORE the op_id re-point), the
        committed leg: the retry's journal entry and queue op landed, the
        flush persisted the COMMITTED receipt — but the host crashed
        between record_retry and the enqueue-time intent re-pointing, so
        the cas_stale intent still names the ORIGINAL capture's op.  The
        identity match (target+key+namespace+exact digest off the durable
        retry journal entry) must settle that intent too — the receipt's
        op id alone can never match it."""
        op_a = "kl-retry-prerepoint-op"
        op_o = "kl-original-capture-op"
        digest = self._seed_crashed_retry_flush(
            retry_op=op_a, intent_op=op_o,
            receipt={"op_id": op_a, "target": _TARGET, "key": _KEY,
                     "status": "committed", "link_status": None,
                     "version": 1, "row_id": "row-" + op_a,
                     "error": None,
                     "received_at": "2026-01-03T00:00:00Z"})

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 0,
                         "the committed-receipt recovery flush exits 0: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the restart leg reconciles the persisted "
                         "committed retry receipt: %r" % (fbody,))
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "applied",
                         "the retry's journal entry advances to applied: %r"
                         % (entries[-1],))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "applied",
            "window B: the identity fallback settles the intent that "
            "still names the ORIGINAL capture's op (the receipt's op id "
            "alone can never match it): %r" % (intent,))
        self.assertEqual(intent.get("op_id"), op_a,
                         "the settled intent names the durable RETRY op "
                         "that actually landed: %r" % (intent,))
        self.assertEqual(intent.get("row_id"), "row-" + op_a,
                         "the settled intent carries the receipt's "
                         "authoritative row_id")
        self.assertEqual(intent.get("digest"), digest,
                         "the settled intent keeps the staked digest")

        # The stake is spent: a repeat retry refuses typed.
        delivery_before = self._delivery_path.read_bytes()
        again = self._retry()
        self.assertEqual(
            again.returncode, 1,
            "the repeat retry must refuse typed after the pre-re-point "
            "committed settlement; got %d stdout=%r"
            % (again.returncode, again.stdout))
        body = json.loads(again.stdout)
        self.assertIn("already applied", str(body.get("error", "")),
                      "the refusal names the applied state: %r"
                      % (body.get("error"),))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused repeat retry re-enqueues NOTHING")

    def test_restart_recovery_rejected_receipt_settles_pre_repoint_intent(self):
        """FB-017 stage B, window B (crash BEFORE the op_id re-point), the
        rejected leg — the reviewer's exact objection: a rejected retry
        receipt for an intent still naming the ORIGINAL op used to fall
        through to the quarantine fallback, which terminalized the
        journal while leaving the stale cas_stale intent SELECTABLE
        forever.  The tolerant match must settle the intent to terminal
        ``rejected`` off the receipt, and the repeat retry refuses typed
        without re-enqueueing."""
        op_a = "kl-retry-prerepoint-rej-op"
        op_o = "kl-original-capture-op"
        self._seed_crashed_retry_flush(
            retry_op=op_a, intent_op=op_o,
            receipt={"op_id": op_a, "target": _TARGET, "key": _KEY,
                     "status": "rejected", "link_status": "rejected",
                     "error": "namespace_not_allowed", "row_id": None,
                     "version": 1,
                     "received_at": "2026-01-03T00:00:00Z"})

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 1,
                         "the settled-rejection flush exits 1: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the restart leg reconciles the persisted "
                         "rejected retry receipt: %r" % (fbody,))
        self.assertEqual(
            fbody.get("journal_quarantined") or [], [op_o],
            "the quarantine envelope names the settled intent's op (the "
            "pre-re-point intent still names the ORIGINAL capture's op): "
            "%r" % (fbody,))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "rejected",
            "window B: the tolerant match settles the intent still naming "
            "the ORIGINAL capture's op (the old op_id-only match could "
            "never find it, and the fallback left the stale stake "
            "selectable): %r" % (intent,))
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "quarantined",
                         "the retry's journal entry is terminal "
                         "quarantined: %r" % (entries[-1],))
        self.assertEqual(
            [e for e in entries if e["seq"] == 1][0]["state"],
            "quarantined",
            "the ORIGINAL quarantined entry survives verbatim below it")

        # The stake is terminal: a repeat retry refuses typed (the cause
        # is not a stale CAS) and re-enqueues NOTHING.
        delivery_before = self._delivery_path.read_bytes()
        again = self._retry()
        self.assertEqual(
            again.returncode, 1,
            "the repeat retry must refuse typed after the pre-re-point "
            "rejected settlement; got %d stdout=%r"
            % (again.returncode, again.stdout))
        body = json.loads(again.stdout)
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", ""))
        self.assertIn("rejected", error,
                      "the refusal names the quarantine cause: %r"
                      % (error,))
        self.assertIn("stale CAS", error,
                      "the refusal names the recoverable-cause rule: %r"
                      % (error,))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused repeat retry re-enqueues NOTHING")

    def test_restart_recovery_rejected_receipt_settles_quarantined_entry(
            self):
        """FB-017 FINAL rework (rejection-side crash window): a flush
        quarantined the retry's journal entry — the FIRST step of
        _reconcile_rejected — and crashed BEFORE settling the stake,
        leaving journal ``quarantined`` + intent ``cas_stale`` + the
        persisted NON-CAS rejected receipt + an EMPTY queue.  The
        pointer_queued selection could never resume (the quarantine spent
        its only recovery key), so the stale stake stayed selectable
        forever and `knowledge retry` re-enqueued a fresh op for a cause
        that can never land.  The next flush settles the intent to
        terminal ``rejected`` off the QUARANTINED retry entry + receipt
        (exact stake identity), re-marks NOTHING on the journal, recovers
        nothing on the repeat flush, and the repeat retry refuses typed
        without re-enqueueing."""
        op_a = "kl-retry-rejquar-op"
        self._seed_crashed_retry_flush(
            retry_op=op_a, intent_op=op_a, retry_state="quarantined",
            receipt={"op_id": op_a, "target": _TARGET, "key": _KEY,
                     "status": "rejected", "link_status": "rejected",
                     "error": "namespace_not_allowed", "row_id": None,
                     "version": 1,
                     "received_at": "2026-01-03T00:00:00Z"})
        journal_before = self._journal()["entries"]
        self.assertEqual(
            [e for e in journal_before if e["seq"] == 2][0]["state"],
            "quarantined",
            "seed sanity: the retry's journal entry is ALREADY quarantined "
            "(the crash happened after the journal quarantine, before the "
            "stake settlement)")

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 1,
                         "the settled-rejection flush exits 1: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the restart leg reconciles the persisted rejected "
                         "retry receipt off the QUARANTINED entry: %r"
                         % (fbody,))
        self.assertEqual(
            fbody.get("journal_quarantined") or [], [op_a],
            "the quarantine envelope names the settled intent's op (no "
            "fresh journal mark — the entry was already terminal): %r"
            % (fbody,))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "rejected",
            "the rejection-side crash window settles the cas_stale intent "
            "to terminal ``rejected`` off the quarantined retry entry + "
            "receipt (the pointer_queued-only selection left it selectable "
            "forever): %r" % (intent,))
        self.assertEqual(intent.get("op_id"), op_a,
                         "the settled intent names the retry op")
        self.assertEqual(
            (self._delivery()["receipts"].get(op_a) or {}).get("error"),
            "namespace_not_allowed",
            "the NON-CAS machine cause stays retained on the receipt: %r"
            % (self._delivery()["receipts"].get(op_a),))
        self.assertEqual(
            self._journal()["entries"], journal_before,
            "the quarantined retry entry is NOT re-marked and the "
            "quarantined ORIGINAL survives verbatim (nothing advanced, "
            "nothing re-enqueued)")

        # Idempotency: a second flush recovers NOTHING and both sidecars
        # stay byte-identical.
        delivery_after = self._delivery_path.read_bytes()
        journal_after = self._journal_path.read_bytes()
        reflushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(reflushed.returncode, 0,
                         "the repeat flush has nothing left to settle: %r"
                         % (reflushed.stdout,))
        rbody = json.loads(reflushed.stdout)
        self.assertIs(rbody.get("ok"), True,
                      "nothing pending, rejected or quarantined: %r"
                      % (rbody,))
        self.assertEqual(rbody.get("recovered_from_receipts"), 0,
                         "the settled stake is terminal: the second flush "
                         "recovers NOTHING: %r" % (rbody,))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_after,
                         "the repeat flush leaves the delivery sidecar "
                         "byte-identical")
        self.assertEqual(self._journal_path.read_bytes(), journal_after,
                         "the repeat flush leaves the journal byte-identical")

        # The stake is terminal: a repeat retry refuses typed (the cause
        # is not a stale CAS) and re-enqueues NOTHING.
        again = self._retry()
        self.assertEqual(
            again.returncode, 1,
            "the repeat retry must refuse typed after the rejection-side "
            "settlement; got %d stdout=%r" % (again.returncode, again.stdout))
        body = json.loads(again.stdout)
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", ""))
        self.assertIn("rejected", error,
                      "the refusal names the quarantine cause: %r"
                      % (error,))
        self.assertIn("stale CAS", error,
                      "the refusal names the recoverable-cause rule: %r"
                      % (error,))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_after,
                         "the refused repeat retry re-enqueues NOTHING")

    def test_restart_recovery_cross_wired_op_refuses_to_guess(self):
        """FB-017 stage B, the conservative guard: when a cas_stale intent
        in a DIFFERENT slot also names the retry op (a cross-wired
        sidecar), the receipt's owner is ambiguous — the recovery must
        NOT guess.  The journal still advances off the authoritative
        committed receipt, but the identity's cas_stale intent is left
        UNTOUCHED (no settlement) rather than advanced on a guess."""
        op_a = "kl-retry-crosswired-op"
        op_o = "kl-original-capture-op"
        digest = self._seed_crashed_retry_flush(
            retry_op=op_a, intent_op=op_o,
            receipt={"op_id": op_a, "target": _TARGET, "key": _KEY,
                     "status": "committed", "link_status": None,
                     "version": 1, "row_id": "row-" + op_a,
                     "error": None,
                     "received_at": "2026-01-03T00:00:00Z"})
        # The cross-wired neighbor: a DIFFERENT logical key whose
        # cas_stale intent names THIS retry's op.
        other_digest = _digest("cross wired neighbor body")
        delivery = self._delivery()
        delivery["intents"]["%s|%s" % (
            _NAMESPACE, "knowledge:%s:guides/other" % _TARGET)] = {
            "target": _TARGET, "key": "guides/other",
            "namespace": _NAMESPACE,
            "digest": other_digest,
            "locator": _locator("guides/other", other_digest),
            "version": 1, "expected_row_id": None, "row_id": None,
            "state": "cas_stale", "op_id": op_a, "staked": True,
            "ref": "knowledge:%s:guides/other" % _TARGET,
            "updated_at": "2026-01-03T00:00:00Z",
        }
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 0,
                         "the committed receipt itself is not in doubt "
                         "(exit 0): %r" % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the restart leg reconciles the receipt: %r"
                         % (fbody,))
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "applied",
                         "the journal advances off the authoritative "
                         "committed receipt: %r" % (entries[-1],))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "cas_stale",
            "the AMBIGUOUS owner is refused: the identity's intent is "
            "left untouched (never advanced on a guess): %r" % (intent,))
        self.assertEqual(intent.get("op_id"), op_o,
                         "the untouched intent keeps its original op")
        self.assertEqual(intent.get("digest"), digest)
        neighbor = self._delivery()["intents"]["%s|%s" % (
            _NAMESPACE, "knowledge:%s:guides/other" % _TARGET)]
        self.assertEqual(neighbor.get("state"), "cas_stale",
                         "the cross-wired neighbor is never advanced (a "
                         "different logical key): %r" % (neighbor,))
        self.assertEqual(neighbor.get("op_id"), op_a,
                         "the neighbor keeps its (cross-wired) op id")

    def test_restart_recovery_applied_journal_settles_final_crash_window(self):
        """FB-017 FINAL crash window: a flush marked the retry's journal
        entry ``applied`` and crashed BEFORE settling the stake — journal
        ``applied`` + committed receipt + intent still ``cas_stale`` +
        EMPTY queue.  The pointer_queued-only selection had spent its only
        recovery key and could never resume, stranding the stale stake
        selectable forever.  The applied RETRY entry itself (op_id +
        retry_ref, exact identity + digest match) must resume the
        settlement from the persisted committed receipt — IDEMPOTENTLY:
        the terminal journal is never re-marked, a second flush neither
        double-settles nor clobbers the settled row, the quarantined
        original survives verbatim, and a repeat retry refuses typed."""
        op_a = "kl-retry-finalwindow-op"
        digest = self._seed_crashed_retry_flush(
            retry_op=op_a, intent_op=op_a, retry_state="applied",
            receipt={"op_id": op_a, "target": _TARGET, "key": _KEY,
                     "status": "committed", "link_status": None,
                     "version": 1, "row_id": "row-" + op_a,
                     "error": None,
                     "received_at": "2026-01-03T00:00:00Z"})
        journal_before = self._journal()["entries"]
        self.assertEqual(
            [e for e in journal_before if e["seq"] == 2][0]["state"],
            "applied",
            "seed sanity: the retry's journal entry is ALREADY applied "
            "(the crash happened after the applied mark)")

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 0,
                         "the final-window recovery flush exits 0: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertIs(fbody.get("ok"), True,
                      "nothing pending, rejected or quarantined: %r"
                      % (fbody,))
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the applied-entry selection resumes the "
                         "settlement off the committed receipt: %r"
                         % (fbody,))
        self.assertEqual(self._journal()["entries"], journal_before,
                         "the terminal journal is NOT re-marked (the "
                         "applied retry entry and the quarantined "
                         "original survive verbatim)")
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "applied",
            "the FINAL window settles the cas_stale intent off the "
            "applied retry entry + committed receipt (the stale stake is "
            "no longer selectable): %r" % (intent,))
        self.assertEqual(intent.get("op_id"), op_a,
                         "the settled intent names the durable retry op")
        self.assertEqual(intent.get("row_id"), "row-" + op_a,
                         "the settled intent carries the receipt's "
                         "authoritative row_id: %r" % (intent,))
        self.assertEqual(intent.get("digest"), digest,
                         "the settled intent keeps the staked digest")

        # IDEMPOTENT: a second flush re-selects NOTHING (the stake is
        # settled; the terminal entry is skipped) — no double settle, no
        # clobber of the settled row, no journal churn.
        delivery_after_settle = self._delivery_path.read_bytes()
        again = self._cli("knowledge", "flush", "--json")
        self.assertEqual(again.returncode, 0,
                         "the repeat flush stays green: %r"
                         % (again.stdout,))
        abody = json.loads(again.stdout)
        self.assertIs(abody.get("ok"), True)
        self.assertEqual(abody.get("recovered_from_receipts"), 0,
                         "the settled stake is never re-counted: %r"
                         % (abody,))
        self.assertEqual(self._delivery_path.read_bytes(),
                         delivery_after_settle,
                         "the repeat flush writes NOTHING to the sidecar "
                         "(no double settle, no clobber)")
        self.assertEqual(self._journal()["entries"], journal_before,
                         "the repeat flush leaves the journal untouched")
        intent = self._intent(_KEY)
        self.assertEqual(intent.get("state"), "applied",
                         "the settlement survives the repeat flush")
        self.assertEqual(intent.get("row_id"), "row-" + op_a,
                         "the settled row_id survives the repeat flush")

        # The stake is spent: a repeat retry refuses typed and re-enqueues
        # NOTHING (the latest journal entry is applied).
        delivery_before = self._delivery_path.read_bytes()
        refused = self._retry()
        self.assertEqual(
            refused.returncode, 1,
            "the repeat retry must refuse typed after the final-window "
            "settlement; got %d stdout=%r"
            % (refused.returncode, refused.stdout))
        body = json.loads(refused.stdout)
        self.assertIs(body.get("ok"), False)
        self.assertIn("already applied", str(body.get("error", "")),
                      "the refusal names the applied state: %r"
                      % (body.get("error"),))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused repeat retry re-enqueues NOTHING")

    def test_restart_recovery_old_receipt_settles_only_exact_original_stake(self):
        """FB-017 FINAL rework (stage B reviewer objection, ownership
        binding): a historical COMMITTED retry receipt must settle ONLY
        the intent for the SAME ORIGINAL STAKE — exact stake VERSION (and
        row identity) — never a LATER stale stake of the same content.

        The attack this pins: the old retry's committed receipt (version
        2, row-old) survives in the bounded receipt map while the
        identity's intent slot is RE-STAKED at version 5 after the
        pointer moved on (an intervening newer stake for the same
        key+digest, itself cas_stale).  The pre-fix matcher accepted any
        cas_stale intent with the same target/key/namespace/digest —
        regardless of stake version — and the settlement's live check
        accepted that intent's OWN op id, so the old receipt settled the
        NEWER stake applied with the OLD row.  Here: the receipt settles
        the exact-version stake (arm 1), recovers NOTHING against the
        newer stake (arm 2 — untouched, still cas_stale), and the newer
        stake stays retryable (arm 3 — `knowledge retry` re-enqueues ITS
        stake verbatim)."""
        op_old = "kl-retry-stakebind-old-op"
        op_orig = "kl-original-capture-op"
        op_new = "kl-newer-stake-op"
        # The post-crash FINAL-window sidecars with a NON-trivial stake
        # (version 2 against row-live-1) and the receipt recording THAT
        # stake's version; the intent names the ORIGINAL capture's op (the
        # crash-before-re-point window).
        digest = self._seed_crashed_retry_flush(
            retry_op=op_old, intent_op=op_orig,
            stake_version=2, stake_expected_row_id="row-live-1",
            retry_state="applied",
            receipt={"op_id": op_old, "target": _TARGET, "key": _KEY,
                     "status": "committed", "link_status": None,
                     "version": 2, "row_id": "row-old",
                     "error": None,
                     "received_at": "2026-01-03T00:00:00Z"})

        # Arm 1 — the EXACT original stake (intent version 2 == the
        # receipt's version 2, pre-repoint op window): the OLD receipt
        # settles it and nothing else stands in the way.
        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 0,
                         "the exact-stake recovery flush exits 0: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the receipt recovers the EXACT-version stake: %r"
                         % (fbody,))
        intent = self._intent(_KEY)
        self.assertEqual(intent.get("state"), "applied",
                         "arm 1: the exact-original-stake intent settles "
                         "applied: %r" % (intent,))
        self.assertEqual(intent.get("row_id"), "row-old",
                         "arm 1: the settlement carries the receipt's "
                         "authoritative row_id: %r" % (intent,))
        self.assertEqual(intent.get("op_id"), op_old,
                         "arm 1: the settled intent names the durable "
                         "retry op: %r" % (intent,))

        # Arm 2 — the INTERVENING NEWER STAKE: after the v2 settlement the
        # identity re-captured the SAME content (same digest) once the
        # pointer had moved on; the fresh stake (version 5 against
        # row-live-4) went cas_stale again, and the OLD version-2 receipt
        # is still retained in the bounded receipt map.  The old receipt
        # must settle NOTHING here.
        seeded = self._seed_stake(_KEY, "beta body", "row-live-4", 5,
                                  state="cas_stale")
        self.assertEqual(seeded["digest"], digest,
                         "fixture sanity: the newer stake is the SAME "
                         "content (digest) at a different version")
        delivery = self._delivery()
        delivery["intents"]["%s|%s" % (_NAMESPACE,
                                       "knowledge:%s:%s" % (_TARGET,
                                                            _KEY))]["op_id"] \
            = op_new
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")
        # The newer stake's own journal cycle: the v2 retry entry is
        # terminal applied (seq 2), so record_intent appended a fresh
        # intent that staked (5, row-live-4) and quarantined cas_stale.
        journal = self._journal()
        journal["entries"].append({
            "seq": 3, "target_id": _TARGET, "logical_key": _KEY,
            "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
            "locator": seeded["locator"], "digest": digest,
            "state": "quarantined",
            "updated_at": "2026-01-05T00:00:00.000+00:00",
            "namespace": _NAMESPACE,
            "summary": _EXPLICIT_SUMMARY,
            "staked_at": "2026-01-05T03:04:05Z",
        })
        self._journal_path.write_text(json.dumps(journal, indent=1),
                                      encoding="utf-8")
        journal_before = self._journal()["entries"]

        flushed2 = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed2.returncode, 0,
                         "nothing is pending or quarantined by the REFUSED "
                         "recovery (exit 0): %r" % (flushed2.stdout,))
        fbody2 = json.loads(flushed2.stdout)
        self.assertIs(fbody2.get("ok"), True, fbody2)
        self.assertEqual(fbody2.get("recovered_from_receipts"), 0,
                         "the OLD version-2 receipt recovers NOTHING once "
                         "the slot holds the version-5 stake: %r" % (fbody2,))
        intent2 = self._intent(_KEY)
        self.assertEqual(intent2.get("state"), "cas_stale",
                         "arm 2: the NEWER stake is NOT settled by the old "
                         "receipt (the pre-fix bug settled it applied with "
                         "the OLD row): %r" % (intent2,))
        self.assertEqual(intent2.get("version"), 5,
                         "arm 2: the newer stake keeps ITS version: %r"
                         % (intent2,))
        self.assertEqual(intent2.get("expected_row_id"), "row-live-4",
                         "arm 2: the newer stake keeps ITS row identity: %r"
                         % (intent2,))
        self.assertEqual(intent2.get("op_id"), op_new,
                         "arm 2: the newer stake keeps ITS op: %r"
                         % (intent2,))
        self.assertIsNone(intent2.get("row_id"),
                          "arm 2: no foreign row_id is stamped onto the "
                          "newer stake: %r" % (intent2,))
        self.assertEqual(self._journal()["entries"], journal_before,
                         "arm 2: the journal is untouched (the terminal "
                         "retry entry is not re-marked, the quarantined "
                         "originals survive verbatim)")

        # Arm 3 — the newer stake stays RETRYABLE: `knowledge retry`
        # re-selects the quarantined seq-3 entry and re-enqueues ITS stake
        # (version 5 + row-live-4) verbatim.
        retried = self._retry()
        self.assertEqual(retried.returncode, 0,
                         "arm 3: the newer cas_stale stake is still "
                         "retryable; stdout=%r stderr=%r"
                         % (retried.stdout[-300:], retried.stderr[-300:]))
        rbody = json.loads(retried.stdout)
        self.assertIs(rbody.get("queued"), True,
                      "arm 3: the retry re-enqueues (the recoverable "
                      "cas_stale cause survived the refused old receipt): "
                      "%r" % (rbody,))
        entries = self._entries_for(_KEY)
        queued_retry = [e for e in entries
                        if e.get("state") == "pointer_queued"
                        and e.get("retry_ref") == 3]
        self.assertEqual(len(queued_retry), 1,
                         "arm 3: exactly one pointer_queued retry entry "
                         "re-enqueues the seq-3 stake: %r" % (entries,))
        queue = [e for e in self._delivery()["queue"]
                 if e.get("key") == _KEY]
        self.assertEqual(len(queue), 1,
                         "arm 3: exactly one queued op for the identity: %r"
                         % (self._delivery()["queue"],))
        self.assertEqual(queue[0].get("version"), 5,
                         "arm 3: the re-enqueued op stakes the NEWER "
                         "version verbatim: %r" % (queue[0],))
        self.assertEqual(queue[0].get("expected_row_id"), "row-live-4",
                         "arm 3: the re-enqueued op stakes the NEWER row "
                         "identity verbatim: %r" % (queue[0],))

    # -- FB-017 rework round 2: REAL receipts carry the stake row identity --

    def _real_crashed_retry_flush(self, *, stake_row: str,
                                  stake_version: int,
                                  reject_error: str | None = None
                                  ) -> tuple[str, str]:
        """The REAL-receipt crash window (FB-017 round 2): a seeded
        cas_stale stake (the documented crash-recovery sidecar format)
        drives a REAL ``knowledge retry`` (the CLI enqueues a fresh
        durable op carrying the ORIGINAL stake verbatim and re-points the
        intent at it) and then a REAL delivery flush — ``flush_to_hub``
        in-process, the exact lane ``workflow.flush`` drives — that
        applies (or, with ``reject_error``, rejects) the op, persists its
        receipt and empties the queue.  The reconciliation never runs
        here (the flush "crashes" between the persisted receipt and it);
        the NEXT CLI flush resumes it.

        Returns ``(retry_op_id, digest)``.  Asserts the REAL receipt
        persists the stake identity: version AND ``expected_row_id``."""
        hub = _FakeHubServer()
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        seeded = self._seed_stake(_KEY, "beta body", stake_row,
                                  stake_version, state="cas_stale")
        # The quarantined ORIGINAL the REAL retry selects.
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [
                {"seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                 "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                 "locator": seeded["locator"], "digest": seeded["digest"],
                 "state": "quarantined",
                 "updated_at": "2026-01-01T00:00:00.000+00:00",
                 "namespace": _NAMESPACE,
                 "summary": _EXPLICIT_SUMMARY,
                 "staked_at": "2026-01-02T03:04:05Z"},
            ]}, indent=1), encoding="utf-8")

        retried = self._retry()
        self.assertEqual(
            retried.returncode, 0,
            "the REAL retry enqueues the recovery op: %s%s"
            % (retried.stdout[-300:], retried.stderr[-300:]))
        rbody = json.loads(retried.stdout)
        self.assertIs(rbody.get("queued"), True,
                      "the retry queues its op for the flush lane: %r"
                      % (rbody,))
        op_id = str(rbody.get("op_id"))
        queue = [e for e in self._delivery()["queue"]
                 if e.get("key") == _KEY]
        self.assertEqual(len(queue), 1,
                         "exactly one queued retry op: %r"
                         % (self._delivery()["queue"],))
        self.assertEqual(queue[0].get("version"), stake_version,
                         "the queued retry op stakes the ORIGINAL version "
                         "verbatim: %r" % (queue[0],))
        self.assertEqual(queue[0].get("expected_row_id"), stake_row,
                         "the queued retry op stakes the ORIGINAL row "
                         "identity verbatim: %r" % (queue[0],))

        # The REAL delivery flush: applies (or rejects) the op, persists
        # the receipt, empties the queue — and stops before the
        # reconciliation (that belongs to workflow.flush, which the NEXT
        # CLI call resumes).
        if reject_error is not None:
            _FakeMCPHub.reject_error = reject_error
        scripts = str(SCRIPTS_DIR)
        sys.path.insert(0, scripts)
        try:
            from knowledge.delivery import (
                KnowledgeDelivery, delivery_path_for_store, flush_to_hub)
            from knowledge import workflow as _workflow

            http = _workflow.build_hub_transport(self.env)
            real_delivery = KnowledgeDelivery(
                delivery_path_for_store(self.tmp))
            raw = flush_to_hub(real_delivery, http)
        finally:
            sys.path.remove(scripts)
        self.assertEqual(real_delivery.pending(), [],
                         "the queue is empty (the crash window: receipt "
                         "persisted, reconciliation skipped): %r" % (raw,))
        receipt = self._delivery()["receipts"].get(op_id)
        self.assertIsNotNone(receipt,
                             "the REAL flush persisted the receipt")
        if reject_error is not None:
            self.assertEqual(
                [r.get("op_id") for r in raw.get("rejected") or []],
                [op_id],
                "the REAL flush rejected exactly the retry op: %r" % (raw,))
            self.assertEqual(receipt.get("status"), "rejected",
                             "the hub rejected the op: %r" % (receipt,))
            self.assertEqual(receipt.get("error"), reject_error,
                             "the rejection receipt names the machine "
                             "cause: %r" % (receipt,))
        else:
            self.assertEqual(raw.get("applied"), [op_id],
                             "the REAL flush applied exactly the retry op: "
                             "%r" % (raw,))
            self.assertEqual(receipt.get("status"), "committed",
                             "the fake hub commits the op: %r" % (receipt,))
        self.assertEqual(receipt.get("version"), stake_version,
                         "the REAL receipt persists the stake VERSION: %r"
                         % (receipt,))
        self.assertEqual(receipt.get("expected_row_id"), stake_row,
                         "FB-017 round 2: the REAL receipt persists the "
                         "stake's ROW identity — without it, recovery "
                         "cannot tell same-version same-digest stakes "
                         "apart: %r" % (receipt,))
        return op_id, seeded["digest"]

    def _intervene_same_version_stake(self, *, digest: str,
                                      row: str, version: int,
                                      op: str) -> None:
        """The reviewer's intervening stake: the identity's live intent
        slot is RE-STAKED at the SAME version and SAME digest against a
        DIFFERENT predecessor row (the pointer row was replaced — only
        the row identity tells the stakes apart), state cas_stale naming
        its own op, plus the quarantined journal cycle that makes it the
        selectable stake."""
        seeded = self._seed_stake(_KEY, "beta body", row, version,
                                  state="cas_stale")
        self.assertEqual(seeded["digest"], digest,
                         "fixture sanity: the intervening stake is the "
                         "SAME content (digest) at the same version")
        delivery = self._delivery()
        delivery["intents"]["%s|%s" % (_NAMESPACE,
                                       "knowledge:%s:%s" % (_TARGET,
                                                            _KEY))]["op_id"] \
            = op
        self._delivery_path.write_text(json.dumps(delivery, indent=1),
                                       encoding="utf-8")
        journal = self._journal()
        journal["entries"].append({
            "seq": 3, "target_id": _TARGET, "logical_key": _KEY,
            "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
            "locator": seeded["locator"], "digest": digest,
            "state": "quarantined",
            "updated_at": "2026-01-05T00:00:00.000+00:00",
            "namespace": _NAMESPACE,
            "summary": _EXPLICIT_SUMMARY,
            "staked_at": "2026-01-05T03:04:05Z",
        })
        self._journal_path.write_text(json.dumps(journal, indent=1),
                                      encoding="utf-8")

    def test_real_flush_committed_receipt_never_settles_different_row_stake(self):
        """FB-017 rework round 2, the reviewer's exact scenario driven
        through the REAL remote flush path (no hand-seeded receipt): a
        retry op staked with expected_row_id R1 is applied by a real
        flush that crashes before its reconciliation (the FINAL window:
        its journal mark landed, the stake settlement did not); an
        INTERVENING stake — same version, same digest, DIFFERENT
        expected row R2 — becomes the active cas_stale intent.  The
        retry's committed receipt must NOT settle R2: the row identity
        is required, never guessed from version+digest alone.  The
        receipt itself stays authoritative for the retry's JOURNAL entry
        (applied), the R2 stake stays untouched and retryable, and a
        repeat flush neither re-settles nor clobbers anything."""
        op_a, digest = self._real_crashed_retry_flush(
            stake_row="row-live-1", stake_version=2)
        # The FINAL crash window: the crashed flush DID mark the retry's
        # journal entry applied before dying (the persisted-receipt shape
        # the applied-entry recovery leg resumes from) — the interleaving
        # where a NEWER quarantined cycle (the intervening stake below)
        # already owns the latest slot, so the recovery runs entirely off
        # the applied retry entry + the committed receipt.
        journal = self._journal()
        retry_entries = [e for e in journal["entries"]
                         if e.get("op_id") == op_a]
        self.assertEqual(
            len(retry_entries), 1,
            "seed sanity: the retry's pointer_queued journal entry exists")
        retry_entries[0]["state"] = "applied"
        self._journal_path.write_text(json.dumps(journal, indent=1),
                                      encoding="utf-8")
        self._intervene_same_version_stake(
            digest=digest, row="row-live-2", version=2,
            op="kl-intervening-stake-op")

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 0,
                         "the REFUSED recovery is a clean flush (nothing "
                         "pending, rejected or quarantined): %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertIs(fbody.get("ok"), True, fbody)
        self.assertEqual(fbody.get("recovered_from_receipts"), 0,
                         "the receipt recovers NOTHING against the "
                         "intervening stake: %r" % (fbody,))
        entries = self._entries_for(_KEY)
        self.assertEqual(
            [e for e in entries if e.get("op_id") == op_a][0]["state"],
            "applied",
            "the committed receipt itself is not in doubt (the retry's "
            "journal entry stays applied): %r" % (entries,))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "cas_stale",
            "the SAME-version SAME-digest stake with a DIFFERENT "
            "expected row is NOT settled by the retry's receipt (the "
            "pre-fix matcher — row check dead for real receipts that "
            "never carried the field — settled it applied with the OLD "
            "receipt's row): %r" % (intent,))
        self.assertEqual(intent.get("expected_row_id"), "row-live-2",
                         "the intervening stake keeps ITS row identity: %r"
                         % (intent,))
        self.assertEqual(intent.get("version"), 2,
                         "the intervening stake keeps ITS version: %r"
                         % (intent,))
        self.assertIsNone(intent.get("row_id"),
                          "no foreign row_id is stamped onto the "
                          "intervening stake: %r" % (intent,))
        self.assertEqual(
            [e for e in self._journal()["entries"]
             if e.get("seq") == 3][0]["state"], "quarantined",
            "the intervening stake's own quarantined cycle survives "
            "untouched")

        # IDEMPOTENT refusal: a repeat flush re-settles NOTHING (the
        # settled-looking terminal entry is never re-selected off a
        # foreign stake) and writes NOTHING.
        delivery_after = self._delivery_path.read_bytes()
        journal_after = self._journal()["entries"]
        again = self._cli("knowledge", "flush", "--json")
        self.assertEqual(again.returncode, 0,
                         "the repeat flush stays green: %r"
                         % (again.stdout,))
        abody = json.loads(again.stdout)
        self.assertEqual(abody.get("recovered_from_receipts"), 0,
                         "the refused stake is never re-counted: %r"
                         % (abody,))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_after,
                         "the repeat flush writes NOTHING to the sidecar")
        self.assertEqual(self._journal()["entries"], journal_after,
                         "the repeat flush leaves the journal untouched")

        # The intervening stake stays RETRYABLE: `knowledge retry`
        # re-selects its quarantined cycle and re-enqueues ITS stake
        # (version 2 + row-live-2) verbatim.
        retried = self._retry()
        self.assertEqual(retried.returncode, 0,
                         "the intervening cas_stale stake is still "
                         "retryable; stdout=%r stderr=%r"
                         % (retried.stdout[-300:], retried.stderr[-300:]))
        rbody = json.loads(retried.stdout)
        self.assertIs(rbody.get("queued"), True,
                      "the retry re-enqueues (the stake survived the "
                      "refused old receipt): %r" % (rbody,))
        queue = [e for e in self._delivery()["queue"]
                 if e.get("key") == _KEY]
        self.assertEqual(len(queue), 1,
                         "exactly one queued op for the identity: %r"
                         % (self._delivery()["queue"],))
        self.assertEqual(queue[0].get("expected_row_id"), "row-live-2",
                         "the re-enqueued op stakes the intervening row "
                         "identity verbatim: %r" % (queue[0],))

    def test_real_flush_rejected_receipt_never_settles_different_row_stake(self):
        """FB-017 rework round 2, the REJECTION window on the REAL remote
        flush path: the hub rejects the retry op (non-CAS machine cause),
        the real flush persists the rejected receipt — carrying the
        stake's row identity — and crashes before reconciliation; an
        intervening same-version/same-digest stake with a DIFFERENT
        expected row is the active cas_stale intent.  The rejected
        receipt must NOT settle R2: the matcher refuses to bind it, the
        retry's journal entry takes the CONSERVATIVE quarantine, the
        intervening stake stays untouched and retryable."""
        op_a, digest = self._real_crashed_retry_flush(
            stake_row="row-live-1", stake_version=2,
            reject_error="namespace_not_allowed")
        self._intervene_same_version_stake(
            digest=digest, row="row-live-2", version=2,
            op="kl-intervening-stake-op")

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 1,
                         "the conservative-quarantine flush exits 1 "
                         "(the receipt itself is a rejection): %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the restart leg reconciles the persisted "
                         "rejected retry receipt: %r" % (fbody,))
        self.assertIn(op_a, fbody.get("journal_quarantined") or [],
                      "the retry's journal entry takes the conservative "
                      "quarantine (the matcher refused to bind the "
                      "intervening stake): %r" % (fbody,))
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "cas_stale",
            "the SAME-version SAME-digest stake with a DIFFERENT "
            "expected row is NOT settled by the retry's REJECTED receipt "
            "(the pre-fix matcher — row check dead for real receipts — "
            "settled it terminal rejected): %r" % (intent,))
        self.assertEqual(intent.get("expected_row_id"), "row-live-2",
                         "the intervening stake keeps ITS row identity: %r"
                         % (intent,))
        self.assertEqual(intent.get("op_id"), "kl-intervening-stake-op",
                         "the intervening stake keeps ITS op: %r"
                         % (intent,))
        entries = self._entries_for(_KEY)
        self.assertEqual(entries[-1]["state"], "quarantined",
                         "the intervening stake's quarantined cycle is "
                         "the selectable latest entry: %r" % (entries,))

        # The intervening stake stays RETRYABLE: `knowledge retry`
        # re-enqueues ITS stake (version 2 + row-live-2) verbatim.
        retried = self._retry()
        self.assertEqual(retried.returncode, 0,
                         "the intervening cas_stale stake is still "
                         "retryable; stdout=%r stderr=%r"
                         % (retried.stdout[-300:], retried.stderr[-300:]))
        rbody = json.loads(retried.stdout)
        self.assertIs(rbody.get("queued"), True,
                      "the retry re-enqueues (the stake survived the "
                      "refused rejected receipt): %r" % (rbody,))
        queue = [e for e in self._delivery()["queue"]
                 if e.get("key") == _KEY]
        self.assertEqual(len(queue), 1,
                         "exactly one queued op for the identity: %r"
                         % (self._delivery()["queue"],))
        self.assertEqual(queue[0].get("expected_row_id"), "row-live-2",
                         "the re-enqueued op stakes the intervening row "
                         "identity verbatim: %r" % (queue[0],))

    def test_real_flush_committed_receipt_settles_exact_row_stake(self):
        """FB-017 rework round 2, the exact-stake POSITIVE path on the
        REAL remote flush path: the committed receipt a real flush
        persisted for the retry op settles the ONE stake whose row
        identity it carries — the intent lands ``applied`` with the
        receipt's authoritative row_id while preserving the staked
        expected_row_id — and the stake is spent (a repeat retry refuses
        typed without re-enqueueing)."""
        op_a, digest = self._real_crashed_retry_flush(
            stake_row="row-live-1", stake_version=2)

        flushed = self._cli("knowledge", "flush", "--json")
        self.assertEqual(flushed.returncode, 0,
                         "the exact-stake recovery flush exits 0: %r"
                         % (flushed.stdout,))
        fbody = json.loads(flushed.stdout)
        self.assertIs(fbody.get("ok"), True, fbody)
        self.assertEqual(fbody.get("recovered_from_receipts"), 1,
                         "the receipt recovers the EXACT-row stake: %r"
                         % (fbody,))
        intent = self._intent(_KEY)
        self.assertEqual(intent.get("state"), "applied",
                         "the exact-original-stake intent settles applied: "
                         "%r" % (intent,))
        self.assertEqual(intent.get("row_id"), "row-" + op_a,
                         "the settlement carries the receipt's "
                         "authoritative row_id: %r" % (intent,))
        self.assertEqual(intent.get("expected_row_id"), "row-live-1",
                         "the settlement preserves the staked row "
                         "identity verbatim: %r" % (intent,))
        self.assertEqual(intent.get("version"), 2,
                         "the settlement keeps the staked version: %r"
                         % (intent,))
        self.assertEqual(intent.get("digest"), digest,
                         "the settlement keeps the staked digest")
        entries = self._entries_for(_KEY)
        self.assertEqual(
            [e for e in entries if e.get("op_id") == op_a][0]["state"],
            "applied",
            "the retry's journal entry advances to applied: %r"
             % (entries,))

        # The stake is spent: a repeat retry refuses typed and re-enqueues
        # NOTHING (the latest journal entry is applied).
        delivery_before = self._delivery_path.read_bytes()
        again = self._retry()
        self.assertEqual(
            again.returncode, 1,
            "the repeat retry must refuse typed after the exact-stake "
            "settlement; got %d stdout=%r"
            % (again.returncode, again.stdout))
        body = json.loads(again.stdout)
        self.assertIs(body.get("ok"), False)
        self.assertIn("already applied", str(body.get("error", "")),
                      "the refusal names the applied state: %r"
                      % (body.get("error"),))
        self.assertEqual(self._delivery_path.read_bytes(), delivery_before,
                         "the refused repeat retry re-enqueues NOTHING")

    def test_remote_non_cas_rejection_still_refuses_retry_typed(self):
        """NEGATIVE: a hub rejection whose cause is NOT a stale CAS keeps
        the unrecoverable ``rejected`` intent state; the retry refuses
        typed naming the cause."""
        hub = _FakeHubServer(reject_error="namespace_not_allowed")
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        r = self._cli("knowledge", "capture", "--target", _TARGET,
                      "--key", _KEY, "--namespace", _NAMESPACE,
                      "--content", "beta body", "--summary",
                      _EXPLICIT_SUMMARY, "--json")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        intent = self._intent(_KEY)
        self.assertEqual(
            intent.get("state"), "rejected",
            "a non-CAS hub rejection keeps the plain 'rejected' intent "
            "state (never retryable as cas_stale): %r" % (intent,))

        rr = self._retry()
        self.assertEqual(
            rr.returncode, 1,
            "the non-CAS rejection must refuse the retry typed; got %d "
            "stdout=%r" % (rr.returncode, rr.stdout))
        body = json.loads(rr.stdout)
        self.assertIs(body.get("ok"), False)
        error = str(body.get("error", ""))
        self.assertIn("rejected", error,
                      "the refusal names the quarantine cause: %r" % (error,))
        self.assertIn("stale CAS", error,
                      "the refusal names the recoverable-cause rule: %r"
                      % (error,))

    # -- FB-004: retry exit codes / human output --------------------------------

    def test_remote_typed_refusal_exits_1_with_json_envelope(self):
        """A REMOTE typed retry refusal (selection-time; no hub contact)
        must exit 1 with the JSON envelope on stdout — never the _fail
        exit-2 usage path."""
        self._env(remote=True)   # unreachable URL is fine: selection first
        r = self._retry()
        self.assertEqual(
            r.returncode, 1,
            "a remote typed refusal must exit EXACTLY 1 (the _fail "
            "exit-2 usage path is for config/usage errors only); got %d "
            "stdout=%r" % (r.returncode, r.stdout))
        body = json.loads(r.stdout)
        self.assertIs(body.get("ok"), False)
        self.assertIn("no journal entry", str(body.get("error", "")))

    def test_human_mode_retry_success_and_refusal_produce_output(self):
        """Human mode (no --json): success prints a one-line stderr
        status (stdout stays empty), refusal prints the reason on stderr
        with exit 1 — never a silent success."""
        fx = self._fixture_initial_create_quarantine()
        self._tombstone_competitor(fx["competitor_row"])
        ok = self._cli("knowledge", "retry", "--target", _TARGET,
                       "--key", _KEY, "--namespace", _NAMESPACE)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertFalse(ok.stdout.lstrip().startswith("{"),
                         "human mode stdout carries no JSON envelope: %r"
                         % (ok.stdout,))
        self.assertIn("[zmem] knowledge retry:", ok.stderr,
                      "a one-line human status lands on stderr: %r"
                      % (ok.stderr,))

        # Refusal in human mode: the reason on stderr, exit 1, no JSON
        # envelope on stdout (idempotence-by-state: the second retry
        # refuses).
        second = self._cli("knowledge", "retry", "--target", _TARGET,
                           "--key", _KEY, "--namespace", _NAMESPACE)
        self.assertEqual(second.returncode, 1)
        self.assertFalse(second.stdout.lstrip().startswith("{"),
                         "the human refusal prints no JSON envelope: %r"
                         % (second.stdout,))
        self.assertTrue(second.stderr.strip(),
                        "the refusal reason lands on stderr")
        self.assertIn("[zmem] knowledge:", second.stderr)

        # REMOTE human-mode success: the enqueued op is reported on
        # stderr, exit 0, empty stdout.
        hub = _FakeHubServer()
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        seeded = self._seed_stake(_KEY, "beta body", None, 1,
                                  state="cas_stale")
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [{
                "seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": seeded["locator"], "digest": seeded["digest"],
                "state": "quarantined",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE,
                "summary": _EXPLICIT_SUMMARY,
                "staked_at": "2026-01-02T03:04:05Z",
            }]}, indent=1), encoding="utf-8")
        remote_ok = self._cli("knowledge", "retry", "--target", _TARGET,
                              "--key", _KEY, "--namespace", _NAMESPACE)
        self.assertEqual(remote_ok.returncode, 0,
                         remote_ok.stdout + remote_ok.stderr)
        self.assertEqual(remote_ok.stdout, "")
        self.assertIn("[zmem] knowledge retry:", remote_ok.stderr,
                      "the remote human success reports the queued op: %r"
                      % (remote_ok.stderr,))

    # -- FB-001 remote: the default summary from a LEGACY (summaryless) entry --

    def test_remote_retry_legacy_entry_without_summary_enqueues_default(self):
        """The remote twin of the FB-001 probe: a LEGACY journal entry
        (nine base fields, NO summary key) selects fine, and the
        enqueued retry op carries the FIXED default summary
        ``knowledge note <logical_key>``."""
        hub = _FakeHubServer()
        self.addCleanup(hub.close)
        self._env(remote=True, remote_url=hub.url)
        seeded = self._seed_stake(_KEY, "beta body", None, 1,
                                  state="cas_stale")
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path.write_text(json.dumps({
            "version": 1,
            "entries": [{
                "seq": 1, "target_id": _TARGET, "logical_key": _KEY,
                "ref": "knowledge:%s:%s" % (_TARGET, _KEY),
                "locator": seeded["locator"], "digest": seeded["digest"],
                "state": "quarantined",
                "updated_at": "2026-01-01T00:00:00.000+00:00",
                "namespace": _NAMESPACE}]}, indent=1),
            encoding="utf-8")   # LEGACY shape: no summary/staked_at keys

        r = self._retry()
        self.assertEqual(r.returncode, 0,
                         "a legacy summaryless entry still selects and "
                         "enqueues: %r %r" % (r.stdout[-200:],
                                              r.stderr[-200:]))
        body = json.loads(r.stdout)
        self.assertIs(body.get("ok"), True)
        after = self._delivery()
        ours = [e for e in after["queue"] if e.get("key") == _KEY]
        self.assertEqual(len(ours), 1)
        self.assertEqual(
            ours[0].get("summary"), "knowledge note %s" % _KEY,
            "the enqueued op carries the FIXED default summary "
            "(knowledge note <logical_key>): %r"
            % (ours[0].get("summary"),))


if __name__ == "__main__":
    unittest.main()
