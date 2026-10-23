"""Issue #288 (Workstream T PR 9) — durable typed operation queue.

FROZEN acceptance checks C1-C6, authored from the check-author spec at
.agents/issue-traces/288-op-queue/check-author-spec.md BEFORE the feature
exists. Each check names its standing at both ends:

- AC1 ``test_dependent_op_follows_canonical_id_after_dedup`` — a store
  already holding row X (seeded via ``store.py add``) receives an
  ``apply-ops`` batch [A=add(content=whitespace-variant of X, so the write
  path dedup-folds into X), B=supersede(id_or_dep=A's op_id)]. Receipts[A]
  must be committed with ``canonical_id == X's id`` and ``deduped`` True;
  receipts[B] must be committed, resolving THROUGH the canonical-id map to
  tombstone X itself (``get`` shows ``superseded_at`` set) with NO new row
  (row count unchanged). RED at the stacked head 8c1bd401 (no ``apply-ops``
  subcommand: the subprocess exits 2 with argparse's ``invalid choice:
  'apply-ops'`` and the check FAILs with that text), GREEN after the fix
  (issue #288 AC1: the canonical-id map).
- idempotency ``test_replayed_ops_are_idempotent_by_op_id`` — apply a batch
  [add, supersede], snapshot the store FILE BYTES and row count, replay the
  SAME batch (same op_ids): every receipt must be ``duplicate`` with the
  canonical ids recorded on the first run, and the store file bytes must be
  byte-identical with the row count unchanged (a replay changes NOTHING).
  RED at base (``invalid choice: 'apply-ops'``), GREEN after (issue #288:
  the ``applied_ops`` op_id ledger).
- read-your-writes ``test_read_your_writes_overlays_pending_ops`` — under a
  pinned scratch env, ``fleet.opqueue.enqueue(principal="p1", op="update",
  ...)`` then ``fleet.broker.pending_overlay("p1")`` must render a bounded
  ``[PENDING]`` line carrying the op's content; ``mark_receipts`` with a
  committed receipt removes it. RED at base (``fleet.opqueue`` does not
  exist: ``ModuleNotFoundError``), GREEN after (issue #288: the
  read-your-writes overlay).
- queue redaction ``test_queue_file_never_holds_unredacted_secret`` — an
  enqueued add whose content carries a ``ghp_``+40-alnum token, a Windows
  path and a POSIX path (the t08 contract-v2 two-gate probe: dodge the
  doc-drift HOME_PATH_RE, stay inside redaction's covered roots) must NEVER
  reach the queue file unredacted: the raw token bytes are absent and
  ``[REDACTED_SECRET]`` / ``[REDACTED_PATH]`` placeholders are present on
  disk; a second enqueue with a LOWER ``client_seq`` must raise ValueError
  (the monotonic pin rides here). RED at base (``ModuleNotFoundError`` for
  ``fleet.opqueue``), GREEN after (issue #288: redact BEFORE any disk
  write).
- capabilities ``test_mutations_require_capability`` — via the in-process
  hub (``mcp_server.build_server`` + ``server._tool_manager.call_tool``,
  the t06/t08 pattern) with a ``ZMEM_MCP_TOKENS_FILE`` map of two
  principals (one WITHOUT ``mutate``, one WITH): ``apply_ops`` for the
  no-mutate principal commits adds but rejects supersede/invalidate with
  ``error: "capability_denied"``; the mutate principal commits everything;
  the LEGACY leg (single ``ZMEM_MCP_TOKEN``, ``principal=""``) keeps
  today's allow. RED at base (``apply_ops`` is not a hub tool: the SDK
  raises ``ToolError: Unknown tool: apply_ops``, converted to a clean FAIL
  line), GREEN after (issue #288: capability gates on the op queue).
- AC6 ``test_capture_mode_follows_intent_on_both_routes`` — the SAME
  secret-bearing content written (a) through the LOCAL CLI ``store.py add``
  with NO ``--capture-mode`` flag and (b) through ``store.py apply-ops``
  (an add op, no explicit capture mode) must BOTH store ``[REDACTED_
  SECRET]``-redacted content (intent: explicit tool writes are ``auto``);
  the operator-restore leg — ``ingest-jsonl --allow-tombstones`` (no
  ``--trust``) of a hand-written tombstone row containing the raw token —
  stays UNREDACTED (``manual`` intent preserved; that leg is green at base
  and pins the regression). RED at base: leg (a) stores the token VERBATIM
  (today's manual default) so the intent assertion fails — a valid RED for
  C6 — and leg (b) additionally hits ``invalid choice: 'apply-ops'``.
  GREEN after (issue #288 AC6).

Authored contract the implementation must satisfy (the spec, made
executable): ``store.py apply-ops --in <jsonl-or--> [--json]`` with one op
per line ``{"op_id", "op": add|update|supersede|invalidate, "args",
"depends_on", "client_seq", "principal"}`` returning ``{"ok": true,
"receipts": [{"op_id", "status": committed|rejected|duplicate|blocked,
"canonical_id", "deduped", "error"?}]}``; the ``applied_ops(op_id PRIMARY
KEY, ...)`` ledger makes replays ``duplicate`` no-ops; dependent ops whose
``depends_on`` is not committed are ``blocked`` (never abort the batch);
``fleet/opqueue.py`` (``QUEUE_DIR_NAME = "queue"`` under the broker dir,
per-principal append-only ``ops-<principal>.jsonl``, ``enqueue`` /
``pending`` / ``mark_receipts``, redaction before any disk write,
monotonic ``client_seq``, ``QueueOverflowError`` on bound overflow);
``fleet.broker.pending_overlay``; the hub tool ``apply_ops(ops,
client_protocol=None, deadline_ms=None, principal="")`` gating
supersede/invalidate/update on the ``mutate`` capability for non-legacy
principals; capture mode ``auto`` on every apply-ops op and on the local
CLI add/update defaults, while ``ingest-jsonl`` without ``--trust`` stays
``manual``. The ``applied_ops`` table lands as schema version 19.

Env discipline: every check pins a fresh GUID scratch dir (ZMEM_HOME=repo
root, ZMEM_STORE/ZMEM_DATA at the scratch, ZMEM_MODELS_DIR missing,
ZMEM_MODEL_AUTODOWNLOAD=0, PYTHONIOENCODING=utf-8) AFTER stripping EVERY
ambient ``ZMEM_*`` variable (prefix sweep — this also strips ambient
ZMEM_CAPTURE_MODE, ZMEM_DEDUP_THRESHOLD and the ZMEM_OPQUEUE_* knobs the
feature adds) and BEFORE importing mcp_server or fleet, purging cached
modules on entry AND exit (the t06/t08 store-path-freeze discipline), and
restoring everything afterwards. Every store.py SUBPROCESS gets its own
stripped env with ZMEM_STORE/ZMEM_DATA pinned to the same scratch — the
real ~/.zmem / ~/.zcode stores are never touched, and the queue file must
resolve under the PINNED data dir (never the real home).

Runs standalone (CI runs ``python tests/test_t09_op_queue.py`` directly —
the repo's python lane is a direct-execution loop, NOT pytest) with the
t-series selector contract: no argument runs ALL checks, ``C1``..``C6``
run one check, an unknown selector prints ``NO CHECK <arg>`` to stderr and
exits 2. Skips loudly (visible message, exit 0) only when the ``mcp``
package is absent, which CI prevents by installing
hermes-plugin/server/requirements.txt on both matrix legs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

try:
    import mcp  # noqa: F401  (the SDK import that gates the hub surface)
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs requirements.txt
    MCP_AVAILABLE = False

# Subprocess budget per store.py spawn (cold interpreter + storelib import;
# t08_hub.py uses the same order of magnitude).
_SUBPROC_TIMEOUT_S = 180

# issue #288: the capability vocabulary the tokens-file map accepts
# (hermes-plugin/server/auth.py _ALLOWED_CAPABILITIES) — the no-mutate leg
# carries read/passive/write (everything except mutate).
_CAPS_NO_MUTATE = ["read", "passive", "write"]
_CAPS_WITH_MUTATE = ["read", "passive", "write", "mutate"]


def _purge_cached_modules() -> None:
    """Drop env-derived module singletons (t06/t08 store-path-freeze
    discipline) so each check re-imports them under ITS pin — and no
    sibling check inherits a stale STORE_PATH/queue dir after it."""
    doomed = []
    for name in sys.modules:
        if (name in ("mcp_server", "fleet", "host", "redaction",
                     "schema_meta", "store", "auth", "bind_guard")
                or name.startswith(("fleet.", "storelib.", "store.",
                                    "auth.", "bind_guard."))):
            doomed.append(name)
    for name in doomed:
        del sys.modules[name]


@contextlib.contextmanager
def _pinned_env(prefix: str, *, tokens_file: str | None = None):
    """Fresh GUID scratch + pinned env (t06/t08 discipline).

    EVERY ambient ``ZMEM_*`` variable is stripped (prefix sweep, so ambient
    ZMEM_CAPTURE_MODE / ZMEM_DEDUP_THRESHOLD / ZMEM_OPQUEUE_* / broker knobs
    cannot flip a check) before the pin lands; ZMEM_STORE/ZMEM_DATA point at
    the scratch BEFORE any storelib/fleet/mcp_server import; cached modules
    are purged on entry AND exit; everything is restored afterwards.
    """
    tmp = tempfile.mkdtemp(prefix=f"zmem-t09-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: v for k, v in os.environ.items() if k.startswith("ZMEM_")}
    for key in list(os.environ):
        if key.startswith("ZMEM_"):
            os.environ.pop(key)
    pins = {
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": "t09-check-token",
    }
    if tokens_file is not None:
        # MAP mode (issue #281): the principal map replaces the legacy
        # single token, exactly like tests/test_t06_hello_fb.py's map test.
        pins.pop("ZMEM_MCP_TOKEN")
        pins["ZMEM_MCP_TOKENS_FILE"] = tokens_file
    os.environ.update(pins)
    _purge_cached_modules()
    try:
        yield tmp
    finally:
        _purge_cached_modules()
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            os.environ[key] = value


@contextlib.contextmanager
def _pinned_server(prefix: str, *, tokens_file: str | None = None):
    """The pinned env plus the real in-process hub (t06_hello_fb.py:53-91)."""
    with _pinned_env(prefix, tokens_file=tokens_file) as tmp:
        sys.path.insert(0, str(SERVER_DIR))
        try:
            import mcp_server
            server = mcp_server.build_server(
                host="127.0.0.1", port=0, use_tls=False)
            yield server, mcp_server, tmp
        finally:
            sys.path.remove(str(SERVER_DIR))


def _store_subproc_env(tmp: str) -> dict:
    """A stripped subprocess env with the scratch store pinned (t08_hub
    _seed discipline): no ambient ZMEM_* survives into any child."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZMEM_")}
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_store(tmp: str, *argv: str) -> subprocess.CompletedProcess:
    """Run the real store.py CLI against the scratch store (one spawn per
    call; every spawn gets the stripped+pinned env)."""
    return subprocess.run(
        [sys.executable, str(STORE_PY), *argv],
        capture_output=True, text=True, env=_store_subproc_env(tmp),
        cwd=str(REPO_ROOT), timeout=_SUBPROC_TIMEOUT_S)


def _seed_row(tmp: str, content: str, *, expect: str = "stored") -> str:
    """Seed one row via the add CLI (the documented --json result shape)
    and return its id. ``expect`` defaults to a fresh stored row; C1's
    harness twin (dedup) is verified out-of-band in the check-author run."""
    proc = _run_store(
        tmp, "add", "--namespace", "user:global", "--type", "fact",
        "--content", content, "--json")
    assert proc.returncode == 0, (
        f"store.py add seed failed ({proc.returncode}): "
        f"{(proc.stderr or '')[-400:]}")
    result = json.loads(proc.stdout)
    assert result.get("result") == expect, (
        f"store.py add --json result must be {expect!r}, got: {result!r}")
    assert result.get("id"), result
    return result["id"]


def _get_row(tmp: str, mid: str) -> dict:
    """`store.py get --id` one row (JSON on stdout; exit 1 when absent)."""
    proc = _run_store(tmp, "get", "--id", mid)
    assert proc.returncode == 0, (
        f"store.py get --id {mid} failed ({proc.returncode}): "
        f"{(proc.stderr or '')[-400:]}")
    return json.loads(proc.stdout)


def _row_count(store_path: str) -> int:
    """Read-only row count of the memory table (stdlib sqlite3, URI ro
    mode — the probe never writes, so C2's byte-compare stays honest)."""
    uri = Path(store_path).resolve().as_posix()
    conn = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    finally:
        conn.close()


def _cli_error_text(proc: subprocess.CompletedProcess) -> str:
    """The bounded, ANCHOR-CARRYING error text of a failed store.py run:
    argparse prints ``invalid choice: 'apply-ops'`` on its error line, but
    buries it ahead of a very long choice list — keep every error-bearing
    line plus the tail so the base-RED anchor is always visible."""
    lines = (proc.stderr or "").strip().splitlines()
    interesting = [ln for ln in lines
                   if "error" in ln.lower() or "invalid choice" in ln.lower()]
    parts = [ln[:400] for ln in interesting[:2]]
    if lines and (not parts or lines[-1][:400] not in parts):
        parts.append(lines[-1][:400])
    return " | ".join(parts) if parts else "<empty stderr>"


def _apply_ops(tmp: str, ops: list) -> list:
    """Write the op batch to a scratch JSONL and drive
    ``store.py apply-ops --in <file> --json``; return the receipts.

    At BASE the subcommand does not exist: the subprocess exits 2 with
    argparse's ``invalid choice: 'apply-ops'`` and this helper FAILs with
    that stderr text (the frozen base-RED anchor for C1/C2/C6).
    """
    ops_path = os.path.join(tmp, f"ops-{uuid.uuid4().hex}.jsonl")
    Path(ops_path).write_text(
        "".join(json.dumps(op, ensure_ascii=False) + "\n" for op in ops),
        encoding="utf-8", newline="\n")
    proc = _run_store(tmp, "apply-ops", "--in", ops_path, "--json")
    assert proc.returncode == 0, (
        "store.py apply-ops must exit 0 when every op got a verdict; "
        f"got rc={proc.returncode} stderr={_cli_error_text(proc)!r}")
    payload = json.loads(proc.stdout)
    assert isinstance(payload, dict) and payload.get("ok") is True, (
        f"apply-ops --json must answer {{'ok': true, ...}}, got: "
        f"{str(payload)[:400]}")
    receipts = payload.get("receipts")
    assert isinstance(receipts, list) and len(receipts) == len(ops), (
        f"one receipt per op expected ({len(ops)}), got: {receipts!r}")
    return receipts


def _op(op_id: str, kind: str, args: dict, *, depends_on=None,
        client_seq: int, principal: str = "") -> dict:
    """One apply-ops line, in the frozen wire order."""
    return {"op_id": op_id, "op": kind, "args": args,
            "depends_on": depends_on, "client_seq": client_seq,
            "principal": principal}


def _load_opqueue():
    """Import fleet.opqueue + fleet.broker under the pinned env.

    At BASE fleet.opqueue does not exist: print the traceback (so the
    ``ModuleNotFoundError`` text is part of this run's output, per the
    frozen-check protocol) and convert to a clean FAIL line.
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import fleet.opqueue
        import fleet.broker
        return fleet.opqueue, fleet.broker
    except ModuleNotFoundError:
        traceback.print_exc()
        raise AssertionError(
            "skills/memory/scripts/fleet/opqueue.py does not exist at this "
            "tree — issue #288 (durable typed operation queue) not "
            "implemented") from None
    finally:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:  # pragma: no cover - defensive
            pass


def _require(obj, name: str):
    """A missing contract attribute is a clean FAIL, never an AttributeError
    crash of the whole run."""
    value = getattr(obj, name, None)
    if value is None:
        raise AssertionError(
            f"{type(obj).__module__}.{type(obj).__name__} must expose "
            f"{name!r} (issue #288 op-queue contract)")
    return value


def _call_tool(server, name: str, args: dict):
    """server._tool_manager.call_tool (the t06/t08 in-process pattern).

    At BASE ``apply_ops`` is not a registered tool: the SDK raises
    ``ToolError('Unknown tool: apply_ops')`` — printed (so the base error
    text is part of this run's output) and converted to a clean FAIL line.
    """
    try:
        return asyncio.run(
            server._tool_manager.call_tool(name, args, context=None))
    except AssertionError:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise AssertionError(
            f"hub tool {name!r} could not be called: "
            f"{type(exc).__name__}: {exc}") from None


# -- C1 / AC1 -------------------------------------------------------------------

def test_dependent_op_follows_canonical_id_after_dedup():
    """issue #288 AC1: an op that dedup-folds hands its DEPENDENTS the
    keeper's canonical id, not the folded add's phantom."""
    base_content = ("t09 ac1 probe: the canary endpoint cutover to "
                    "us-east-2 finished on october 12")
    # A whitespace-variant of X's content: the write path normalizes
    # whitespace+case into content_norm, so the add dedup-folds into X at
    # the exact-match threshold (verified at base: store.py add --json on
    # this variant answers {"result": "deduped", "id": <X's id>}).
    variant_content = ("t09   ac1 probe:  the canary endpoint\ncutover to "
                       "US-EAST-2   finished on october 12")

    with _pinned_env("c1") as tmp:
        store_path = os.path.join(tmp, "store.sqlite")
        # Seed X first via the add CLI subprocess (the spec's seeding order).
        xid = _seed_row(tmp, base_content)  # issue #288 AC1
        before_count = _row_count(store_path)

        op_a = uuid.uuid4().hex
        op_b = uuid.uuid4().hex
        receipts = _apply_ops(tmp, [
            _op(op_a, "add", {"type": "fact", "content": variant_content,
                              "namespace": "user:global"},
                client_seq=1),
            _op(op_b, "supersede", {"id_or_dep": op_a,
                                    "reason": "t09 ac1 fold supersede"},
                depends_on=op_a, client_seq=2),
        ])  # issue #288 AC1
        by_op = {r.get("op_id"): r for r in receipts}
        a = by_op.get(op_a)
        b = by_op.get(op_b)
        assert isinstance(a, dict) and isinstance(b, dict), (
            f"one receipt per op_id expected: {receipts!r}")
        assert a.get("status") == "committed", (
            f"the dedup-folding add must be COMMITTED (a fold is a "
            f"successful application), got: {a!r}")
        assert a.get("canonical_id") == xid, (
            f"the folded add's canonical_id must be the KEEPER row's id "
            f"{xid!r}, got: {a.get('canonical_id')!r}")
        assert a.get("deduped") is True, (
            f"a receipt that folded into an existing keeper must carry "
            f"deduped=True, got: {a!r}")
        assert b.get("status") == "committed", (
            f"the supersede naming the fold-add's op_id must resolve "
            f"through the canonical-id map and commit, got: {b!r}")
        # B must have touched ONLY X (the keeper), and no orphan row may
        # exist: X is now tombstoned and the row count is unchanged.
        row = _get_row(tmp, xid)
        assert row.get("superseded_at"), (
            f"X must be superseded via the dependent op (superseded_at "
            f"set); got row: {str(row)[:300]}")
        after_count = _row_count(store_path)
        assert after_count == before_count, (
            f"the batch must not leave a new row (before={before_count}, "
            f"after={after_count}) — the folded add creates nothing and the "
            f"supersede only tombstones X")


# -- C2 / idempotency -----------------------------------------------------------

def test_replayed_ops_are_idempotent_by_op_id():
    """issue #288: a replayed committed op_id is a duplicate that changes
    NOTHING — same verdicts' canonical ids, byte-identical store file."""
    content = ("t09 idempotency probe: replaying a committed batch must be "
               "a no-op recorded in applied_ops")
    op_add = uuid.uuid4().hex
    op_sup = uuid.uuid4().hex

    with _pinned_env("c2") as tmp:
        store_path = os.path.join(tmp, "store.sqlite")
        batch = [
            _op(op_add, "add", {"type": "fact", "content": content,
                                "namespace": "user:global"},
                client_seq=1),
            _op(op_sup, "supersede", {"id_or_dep": op_add,
                                      "reason": "t09 idempotency supersede"},
                depends_on=op_add, client_seq=2),
        ]
        first = _apply_ops(tmp, batch)  # issue #288: the applied_ops ledger
        first_by_op = {r.get("op_id"): r for r in first}
        for op_id in (op_add, op_sup):
            assert isinstance(first_by_op.get(op_id), dict), (
                f"first run: receipts must key by op_id {op_id!r}, got: "
                f"{first!r}")
        assert first_by_op[op_add].get("status") == "committed", (
            f"first run: the add must commit, got: {first_by_op[op_add]!r}")
        assert first_by_op[op_add].get("canonical_id"), (
            f"first run: the committed add must carry a canonical_id, got: "
            f"{first_by_op[op_add]!r}")
        assert first_by_op[op_sup].get("status") == "committed", (
            f"first run: the supersede must commit, got: "
            f"{first_by_op[op_sup]!r}")

        # Snapshot the store FILE BYTES and row count after run 1.
        snapshot_bytes = Path(store_path).read_bytes()
        snapshot_count = _row_count(store_path)

        second = _apply_ops(tmp, batch)  # the SAME batch, same op_ids
        second_by_op = {r.get("op_id"): r for r in second}
        for op_id in (op_add, op_sup):
            assert isinstance(second_by_op.get(op_id), dict), (
                f"replay: receipts must key by op_id {op_id!r}, got: "
                f"{second!r}")
        for receipt in second:
            assert receipt.get("status") == "duplicate", (
                f"every replayed committed op_id must come back "
                f"'duplicate'; got: {receipt!r}")
        for op_id in (op_add, op_sup):
            assert (second_by_op[op_id].get("canonical_id")
                    == first_by_op[op_id].get("canonical_id")), (
                f"the duplicate receipt must carry the canonical id "
                f"recorded on the first run for {op_id}: "
                f"{second_by_op[op_id]!r} vs {first_by_op[op_id]!r}")
        assert Path(store_path).read_bytes() == snapshot_bytes, (
            "a replayed batch must leave the store FILE bytes unchanged "
            "(byte-compare failed — the replay wrote something)")
        assert _row_count(store_path) == snapshot_count, (
            "a replayed batch must not change the row count")


# -- C3 / read-your-writes ------------------------------------------------------

def test_read_your_writes_overlays_pending_ops():
    """issue #288: pending ops overlay the read moments so the writer sees
    their own not-yet-applied state, and clear once receipted."""
    with _pinned_env("c3") as tmp:
        opqueue, broker = _load_opqueue()
        enqueue = _require(opqueue, "enqueue")
        mark_receipts = _require(opqueue, "mark_receipts")
        pending_overlay = _require(broker, "pending_overlay")

        # issue #288: the pending overlay renders the op's content.
        entry = enqueue(principal="p1", op="update",
                        args={"id_or_dep": "any-id",
                              "content": "the new state of the flag"})
        assert isinstance(entry, dict) and entry.get("op_id"), (
            f"enqueue must return the minted entry, got: {entry!r}")
        overlay = pending_overlay("p1")
        assert isinstance(overlay, str), (
            f"pending_overlay must return a string, got: "
            f"{type(overlay).__name__}")
        assert "[PENDING]" in overlay, (
            f"the overlay must carry the exact [PENDING] marker literal, "
            f"got: {overlay[:300]!r}")
        assert "the new state of the flag" in overlay, (
            f"the overlay must render the pending op's content, got: "
            f"{overlay[:300]!r}")
        # issue #288: a committed receipt retires the line.
        mark_receipts("p1", [{"op_id": entry["op_id"],
                              "status": "committed",
                              "canonical_id": "any-id"}])
        after = pending_overlay("p1")
        assert "the new state of the flag" not in after, (
            f"a receipted op must leave the overlay, got: {after[:300]!r}")


# -- C4 / queue redaction -------------------------------------------------------

def test_queue_file_never_holds_unredacted_secret():
    """issue #288: the queue file never holds unredacted secrets —
    redaction happens BEFORE any disk write (contract v2: secrets AND
    emails AND paths), and client_seq is monotonic per principal."""
    token = "ghp_" + "a1B2c3D4e5" * 4  # ghp_ + 40 alnum chars
    # The t08 two-gate rule: a Windows path and a POSIX path that stay
    # INSIDE redaction's covered roots (/var and /private are both listed
    # roots; the drive-letter form is covered by _WINDOWS_PATH_RE) while
    # dodging the doc-drift HOME_PATH_RE — the exact spec literals.
    win_path = "C:\\data\\tokens\\ghp_abcdefghijklmno.txt"
    posix_path = "/var/private/creds.txt"
    content = (f"leak probe: token {token} cached at {win_path} and "
               f"{posix_path}")

    with _pinned_env("c4") as tmp:
        opqueue, _broker = _load_opqueue()
        enqueue = _require(opqueue, "enqueue")

        # issue #288: explicit client_seq so the monotonic pin below has a
        # deterministic floor.
        entry = enqueue(principal="p-secret", op="add",
                        args={"type": "fact", "content": content,
                              "namespace": "user:global"},
                        client_seq=5)
        assert isinstance(entry, dict) and entry.get("op_id"), (
            f"enqueue must return the minted entry, got: {entry!r}")

        # The queue file MUST live under the PINNED scratch (the env
        # discipline forbids ever touching the real ~/.zmem): one JSONL per
        # principal, ops-<principal>.jsonl under the queue dir.
        matches = sorted(Path(tmp).rglob("ops-p-secret.jsonl"))
        assert len(matches) == 1, (
            f"exactly one ops-p-secret.jsonl queue file must exist under "
            f"the PINNED scratch {tmp} (found {len(matches)}; the queue "
            f"must resolve through the pinned store/data dir, never the "
            f"real home)")
        raw = matches[0].read_bytes()
        assert token.encode("utf-8") not in raw, (
            "the raw ghp_ token bytes must never reach the queue file — "
            "redaction happens BEFORE any disk write")
        # A JSON-encoded Windows path doubles its backslashes, so probe a
        # distinctive fragment of each path plus both escaped forms: no
        # encoding of either path may survive.
        for needle in (win_path.encode("utf-8"),
                       win_path.replace("\\", "\\\\").encode("utf-8"),
                       b"abcdefghijklmno",
                       posix_path.encode("utf-8"),
                       b"creds.txt"):
            assert needle not in raw, (
                f"raw path bytes must never reach the queue file "
                f"(probe {needle!r} survived; contract v2 path scrubbing)")
        assert b"[REDACTED_SECRET]" in raw, (
            "the queue line must carry the [REDACTED_SECRET] placeholder, "
            "proving the token was rewritten rather than dropped")
        assert b"[REDACTED_PATH]" in raw, (
            "the queue line must carry the [REDACTED_PATH] placeholder for "
            "both filesystem paths (contract v2)")
        # Parsed-line sweep: no string ANYWHERE in a stored entry (keys
        # included) may carry the token or either path in any position.
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            flat = json.dumps(parsed, ensure_ascii=False)
            assert token not in flat and win_path not in flat, (
                f"no field of a queue entry may hold unredacted material: "
                f"{flat[:300]!r}")
            assert posix_path not in flat, (
                f"no field of a queue entry may hold the raw POSIX path: "
                f"{flat[:300]!r}")

        # issue #288: monotonic client_seq per principal — a LOWER seq is a
        # loud ValueError, never a silent append.
        raised = False
        try:
            enqueue(principal="p-secret", op="add",
                    args={"type": "fact",
                          "content": "t09 monotonic probe second op",
                          "namespace": "user:global"},
                    client_seq=3)  # lower than the 5 recorded above
        except ValueError:
            raised = True
        assert raised, (
            "enqueue with a client_seq lower than the principal's last "
            "recorded seq must raise ValueError (monotonic pin)")


# -- C5 / capabilities ----------------------------------------------------------

def test_mutations_require_capability():
    """issue #288: supersede/invalidate through the hub's apply_ops need
    the `mutate` capability for non-legacy principals; adds never do; the
    legacy principal keeps today's allow."""
    tmp_map = tempfile.mkdtemp(prefix="zmem-t09-c5map-")
    try:
        tokens_path = os.path.join(tmp_map, "tokens.json")
        Path(tokens_path).write_text(json.dumps({
            # issue #288: two principals from the SAME tokens-file map —
            # the hub resolves capabilities BY PRINCIPAL ID (call_tool
            # carries no auth context in-process, so the tool takes an
            # explicit `principal` parameter naming the map entry).
            "t09-nomut": {"principal": "p-nomut", "host": "t09host",
                          "harness": "t09harness",
                          "capabilities": _CAPS_NO_MUTATE},
            "t09-mut": {"principal": "p-mut", "host": "t09host",
                        "harness": "t09harness",
                        "capabilities": _CAPS_WITH_MUTATE},
        }), encoding="utf-8")

        def batch_ops(tag: str) -> list:
            # Two adds so the supersede and the invalidate each own a LIVE
            # target: supersede tombstones its row, and the write path
            # refuses re-tombstoning an already-superseded row — a single
            # shared target could never show the mutate principal's
            # all-committed verdict. The op KINDS are exactly the spec's
            # [add, supersede, invalidate].
            add_a = uuid.uuid4().hex
            add_b = uuid.uuid4().hex
            return [
                _op(add_a, "add",
                    {"type": "fact",
                     "content": f"t09 c5 {tag} live row A",
                     "namespace": "user:global"}, client_seq=1),
                _op(add_b, "add",
                    {"type": "fact",
                     "content": f"t09 c5 {tag} live row B",
                     "namespace": "user:global"}, client_seq=2),
                _op(uuid.uuid4().hex, "supersede",
                    {"id_or_dep": add_a,
                     "reason": f"t09 c5 {tag} supersede"},
                    depends_on=add_a, client_seq=3),
                _op(uuid.uuid4().hex, "invalidate",
                    {"id_or_dep": add_b,
                     "reason": f"t09 c5 {tag} invalidate"},
                    depends_on=add_b, client_seq=4),
            ]

        with _pinned_server("c5map", tokens_file=tokens_path) as \
                (server, _mod, _tmp):
            # issue #288: the no-mutate principal — adds still apply;
            # supersede+invalidate are rejected with the stable code.
            resp = _call_tool(server, "apply_ops", {
                "ops": batch_ops("nomut"), "principal": "p-nomut"})
            assert isinstance(resp, dict) and resp.get("ok") is True, (
                f"apply_ops must answer ok for a fully-verdicted batch, "
                f"got: {str(resp)[:400]}")
            receipts = list(resp.get("receipts", []))
            assert receipts, "apply_ops must return one receipt per op"
            for r in receipts:
                if r.get("op") == "add":
                    assert r.get("status") == "committed", (
                        f"add ops apply regardless of the mutate "
                        f"capability, got: {r!r}")
            for kind in ("supersede", "invalidate"):
                verdicts = [r for r in receipts if r.get("op") == kind]
                assert verdicts, f"a {kind} op receipt is missing: {receipts!r}"
                for r in verdicts:
                    assert r.get("status") == "rejected", (
                        f"the no-mutate principal's {kind} must be "
                        f"REJECTED, got: {r!r}")
                    assert r.get("error") == "capability_denied", (
                        f"the rejection must carry "
                        f"error='capability_denied', got: {r!r}")

            # issue #288: the mutate principal — everything commits.
            resp = _call_tool(server, "apply_ops", {
                "ops": batch_ops("mut"), "principal": "p-mut"})
            assert isinstance(resp, dict) and resp.get("ok") is True, (
                f"apply_ops must answer ok for the mutate principal, got: "
                f"{str(resp)[:400]}")
            for receipt in resp.get("receipts", []):
                assert receipt.get("status") == "committed", (
                    f"the mutate principal's every op must commit, got: "
                    f"{receipt!r}")

        # issue #288: the LEGACY leg — a single-token hub (no principal
        # map) with principal="" keeps today's allow (capabilities is None
        # means legacy, and legacy is allowed).
        with _pinned_server("c5leg") as (server, _mod, _tmp):
            add_a = uuid.uuid4().hex
            ops = [
                _op(add_a, "add",
                    {"type": "fact",
                     "content": "t09 c5 legacy live row",
                     "namespace": "user:global"}, client_seq=1),
                _op(uuid.uuid4().hex, "supersede",
                    {"id_or_dep": add_a, "reason": "t09 c5 legacy supersede"},
                    depends_on=add_a, client_seq=2),
            ]
            resp = _call_tool(server, "apply_ops",
                              {"ops": ops, "principal": ""})
            assert isinstance(resp, dict) and resp.get("ok") is True, (
                f"apply_ops must answer ok for the legacy principal, got: "
                f"{str(resp)[:400]}")
            for receipt in resp.get("receipts", []):
                assert receipt.get("status") == "committed", (
                    f"the legacy principal (capabilities is None) keeps "
                    f"today's allow — its supersede must commit, got: "
                    f"{receipt!r}")
    finally:
        shutil.rmtree(tmp_map, ignore_errors=True)


# -- C6 / AC6 -------------------------------------------------------------------

def test_capture_mode_follows_intent_on_both_routes():
    """issue #288 AC6: capture mode follows the route's intent — the local
    CLI add default AND apply-ops both run auto (same redaction); the
    operator's ingest-jsonl --allow-tombstones restore stays manual
    (verbatim)."""
    token = "ghp_" + "a1B2c3D4e5" * 4  # ghp_ + 40 alnum chars

    with _pinned_env("c6") as tmp:
        # (a) LOCAL CLI route: store.py add with NO --capture-mode flag —
        # the new default is auto (intent: an explicit tool write). At the
        # stacked head this leg stores the token VERBATIM (today's manual
        # default), which is exactly C6's base RED.
        pid = _seed_row(  # issue #288 AC6
            tmp, f"t09 ac6 route-a local add carries {token} in content")
        row_a = _get_row(tmp, pid)
        assert "[REDACTED_SECRET]" in row_a.get("content", ""), (
            f"the local CLI add default must be capture mode auto, so the "
            f"token is stored redacted; got verbatim content: "
            f"{row_a.get('content', '')[:200]!r}")
        assert token not in row_a.get("content", ""), (
            "the raw token must never survive the local add route")

        # (b) OP-QUEUE route: an add op through apply-ops with no explicit
        # capture mode — same auto intent, identical redaction.
        op_id = uuid.uuid4().hex
        receipts = _apply_ops(tmp, [  # issue #288 AC6
            _op(op_id, "add",
                {"type": "fact",
                 "content": f"t09 ac6 route-b apply-ops add carries "
                            f"{token} in content",
                 "namespace": "user:global"},
                client_seq=1)])
        receipt = receipts[0]
        assert receipt.get("status") == "committed", (
            f"the apply-ops add must commit, got: {receipt!r}")
        rid = receipt.get("canonical_id")
        assert isinstance(rid, str) and rid, (
            f"the committed add's receipt must carry its canonical_id, "
            f"got: {receipt!r}")
        assert rid != pid, "route (b) probes a distinct row, not a dedup fold"
        row_b = _get_row(tmp, rid)
        assert "[REDACTED_SECRET]" in row_b.get("content", ""), (
            f"apply-ops must run capture mode auto (identical redaction by "
            f"intent); got content: {row_b.get('content', '')[:200]!r}")
        assert token not in row_b.get("content", ""), (
            "the raw token must never survive the apply-ops route")

    # The operator-restore leg, in a FRESH temp store: ingest-jsonl
    # --allow-tombstones (no --trust) of ONE hand-written tombstone row
    # carrying the raw token must import it VERBATIM — the sync channel
    # keeps its manual intent (this leg is green at the stacked head and
    # pins the regression). Row shape per sync.py's _validate_sync_row
    # parser: {"id": <36-char uuid>, "type" in ALLOWED_TYPES, "content",
    # "namespace", tags/source_ref/signal/confidence/taint optional, and a
    # tombstone carries superseded_at (+ valid_until inherited = same
    # instant; supersede_reason documents it)}.
    with _pinned_env("c6restore") as tmp:
        fresh_id = str(uuid.uuid4())
        when = "2026-10-20T12:00:00Z"
        tombstone_row = {
            "id": fresh_id,
            "type": "fact",
            "content": f"t09 ac6 operator restore carries {token} verbatim",
            "namespace": "user:global",
            "tags": "",
            "source_ref": "",
            "signal": "none",
            "confidence": 0.9,
            "ingestion_ts": when,
            "valid_from": "",
            "valid_until": "",
            "update_of": "",
            "taint": "untrusted_tool",
            "superseded_at": when,
            "supersede_reason": "t09 ac6 operator restore",
        }
        rows_path = os.path.join(tmp, "restore.jsonl")
        Path(rows_path).write_text(
            json.dumps(tombstone_row, ensure_ascii=False) + "\n",
            encoding="utf-8", newline="\n")
        proc = _run_store(  # issue #288 AC6: manual intent preserved
            tmp, "ingest-jsonl", "--in", rows_path, "--allow-tombstones")
        assert proc.returncode == 0, (
            f"ingest-jsonl --allow-tombstones must succeed on a valid "
            f"hand-written export row (rc={proc.returncode}): "
            f"{(proc.stderr or '')[-400:]!r}")
        assert "added=1" in (proc.stdout or ""), (
            f"the summary line must count the tombstone row as added, got: "
            f"{(proc.stdout or '')[-300:]!r}")
        row = _get_row(tmp, fresh_id)
        assert token in row.get("content", ""), (
            f"the operator-restore route keeps MANUAL capture intent — the "
            f"raw token must be stored verbatim; got: "
            f"{row.get('content', '')[:200]!r}")
        assert row.get("superseded_at"), (
            "the restored tombstone row must keep its superseded_at")


# -- standalone runner (CI direct-execution lane) -------------------------------

_CHECKS = {
    "C1": test_dependent_op_follows_canonical_id_after_dedup,
    "C2": test_replayed_ops_are_idempotent_by_op_id,
    "C3": test_read_your_writes_overlays_pending_ops,
    "C4": test_queue_file_never_holds_unredacted_secret,
    "C5": test_mutations_require_capability,
    "C6": test_capture_mode_follows_intent_on_both_routes,
}


def _all_checks():
    return tuple(_CHECKS[selector] for selector in ("C1", "C2", "C3",
                                                    "C4", "C5", "C6"))


def _main(argv: list[str]) -> int:
    if not MCP_AVAILABLE:
        print("SKIP: mcp package absent — tests/test_t09_op_queue.py "
              "requires the 'mcp' package "
              "(hermes-plugin/server/requirements.txt); not installed here.")
        return 0
    selectors = argv[1:]
    for selector in selectors:
        if selector not in _CHECKS:
            print(f"NO CHECK {selector}", file=sys.stderr)
            return 2
    selected = ([_CHECKS[s] for s in selectors] if selectors
                else list(_all_checks()))
    failures = []
    for check in selected:
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
    sys.exit(_main(sys.argv))
