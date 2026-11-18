"""Arm's-length acceptance check: the deepseek plugin bounds its ledger
arrays through the shared state module — HARD bounds with an INDEPENDENT
ORACLE (CS-010, issue trace 480-481-482, Fix 4, rebuild r4).

Published selector: StateBoundsTest.test_arrays_bounded_per_ledger_mechanism
(published argv: python -X utf8 tests/test_deepseek_state_bounds.py
StateBoundsTest.test_arrays_bounded_per_ledger_mechanism).

FROZEN RETENTION LAW (the implementer must satisfy exactly this):

1. NEW pure module ``deepseek-plugin/state.mjs`` (node builtins only)
   exporting:
   - ``CAPS``: constant object, integer keys ``receipts`` and
     ``captureQueue`` (both >= 1) — the production bounds.
   - ``boundReceipts(receipts, cap)``: HARD total bound.  Eviction order:
     oldest TERMINAL first; only when no terminal remains, oldest PENDING
     (pending is DEPRIORITIZED, never exempt).  Retained order preserved.
     Non-array input -> ``[]``; ``cap`` <= 0 -> ``[]``; input never
     mutated.
   - ``boundCaptureQueue(queue, cap, currentSession)``: HARD total bound.
     Eviction order: oldest NON-current-session entries first; only when
     only current-session entries remain and they exceed cap, oldest
     current.  Same purity contracts.
2. ``deepseek-plugin/index.mjs`` bounds BOTH arrays at write time
   (onReceipt routes receipts through ``boundReceipts`` with
   ``state.pendingReceipts`` recomputed from the BOUNDED array;
   capture-queue pushes route through ``boundCaptureQueue`` bound with
   the session of THAT write).

ELIGIBILITY DISCLOSURE (the recency-window contract): the nudge
eligibility signal is "current-session activity within the retained
window" — the bounded output still contains at least one current-session
entry whenever one falls inside the cap-sized newest window.  This is
the honest bounded semantics, disclosed in the plan.

NEVER HAND-COUNTED: every expected survivor list below is derived from a
tiny INDEPENDENT ORACLE — a second Python implementation of the frozen
law (`_oracle_receipts`, `_oracle_queue`, `_oracle_push_sequence`) that
lives in this file.  The pure legs assert state.mjs equals the oracle;
the live legs assert the real plugin's arrays equal the oracle simulated
over the same chronological feed (push-time session binding).

Base expectations (deterministic, observed): every leg RED at base —
the plugin pushes unbounded, the state file never gets bounded on
write, and state.mjs does not exist.

Run: python -X utf8 tests/test_deepseek_state_bounds.py
"""

from __future__ import annotations
import os
import sys
import tempfile

# Scratch env pinned BEFORE the isolation guard.
_TMP = tempfile.mkdtemp(prefix="zmem-dsbounds-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_MJS = REPO_ROOT / "deepseek-plugin" / "state.mjs"
INDEX_MJS = REPO_ROOT / "deepseek-plugin" / "index.mjs"


# ---------------------------------------------------------------------------
# INDEPENDENT ORACLE for the frozen law (a second implementation — the
# expected survivors are always DERIVED from these, never hand-counted).


def _oracle_receipts(receipts, cap):
    """boundReceipts(receipts, cap): hard bound; oldest terminal evicted
    first; only-pending-over-cap evicts oldest pending."""
    if not isinstance(receipts, list) or not isinstance(cap, int) \
            or cap <= 0:
        return []
    keep = list(receipts)
    while len(keep) > cap:
        idx = next((i for i, r in enumerate(keep)
                    if str(r.get("status")) != "pending"), None)
        keep.pop(idx if idx is not None else 0)
    return keep


def _oracle_queue(queue, cap, current_session):
    """boundCaptureQueue(queue, cap, currentSession): hard bound; oldest
    non-current evicted first; only-current-over-cap evicts oldest
    current."""
    if not isinstance(queue, list) or not isinstance(cap, int) or cap <= 0:
        return []
    keep = list(queue)
    while len(keep) > cap:
        idx = next((i for i, e in enumerate(keep)
                    if e.get("session") != current_session), None)
        keep.pop(idx if idx is not None else 0)
    return keep


def _oracle_push_sequence(feed, cap, kind):
    """Simulate the WRITE-TIME law over a chronological feed.  For
    ``kind == "queue"`` each entry is (session, marker) and the law binds
    with THAT write's session as currentSession; for ``kind ==
    "receipts"`` each entry is a status string."""
    state = []
    for item in feed:
        if kind == "queue":
            session, marker = item
            state.append({"session": session, "text": marker})
            state = _oracle_queue(state, cap, session)
        else:
            state.append({"status": item})
            state = _oracle_receipts(state, cap)
    return state


def _markers(entries):
    # First whitespace token of the entry text: the live correction entry
    # carries the full local correction text (marker + body); the oracle
    # feed names the marker token.  Retention identity is the token.
    out = []
    for e in entries:
        text = str(e.get("text") or "")
        out.append(text.split()[0] if text.split() else "")
    return out


def _statuses(entries):
    return [str(e.get("status")) for e in entries]


# ---------------------------------------------------------------- stub tree

_STUB_DSH_LLM = (
    "export function createUserMessage(init) { return { id: \"stub-msg\", "
    "...init }; }\n")

_RESOLVE_HOOK = """
export async function resolve(specifier, context, next) {
  if (specifier === "@deepseek-ai/dsh-llm") {
    return { url: process.env.DSH_LLM_STUB_URL, shortCircuit: true };
  }
  return next(specifier, context);
}
"""

_REGISTER_HOOK = """
import { register } from "node:module";
register(new URL("./stub-resolve.mjs", import.meta.url), import.meta.url);
"""

_STUB_STORE_PY = '''\
import json, os, sys
state_path = os.environ["STUB_STATE"]
state = {"next": 1, "ops": {}}
if os.path.exists(state_path):
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
if "ops-enqueue" in sys.argv:
    n = state["next"]; state["next"] = n + 1
    op_id = "kl-stub-%d" % n
    forced = os.environ.get("STUB_ENQUEUE_STATUS")
    if forced in ("pending", "committed"):
        status = forced
    else:
        status = "pending" if n % 4 == 0 else "committed"
    state["ops"][op_id] = status
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    print(json.dumps({"ok": True, "op_id": op_id, "status": status}))
elif "ops-flush" in sys.argv:
    if os.environ.get("STUB_FLUSH_COMMIT") == "1":
        state["ops"] = {k: "committed" for k in state["ops"]}
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    receipts = [{"op_id": k, "status": v} for k, v in state["ops"].items()]
    print(json.dumps({"ok": True, "receipts": receipts}))
else:
    print("")
'''

_STUB_CAPTURE_ENTRY = '''\
import json, sys
try:
    payload = json.load(sys.stdin)
    text = str(payload.get("text") or "")[:120]
except Exception:
    text = ""
print(json.dumps({"ok": True, "deduped": False, "text": text}))
'''

# ------------------------------------------------------------- pure probe

_PURE_PROBE = """
const mod = await import(process.env.STATE_URL);
const { boundReceipts, boundCaptureQueue, CAPS } = mod;
const out = { capsOk: false, receipts: {}, queue: {} };
out.capsOk = CAPS && Number.isInteger(CAPS.receipts) && CAPS.receipts >= 1
  && Number.isInteger(CAPS.captureQueue) && CAPS.captureQueue >= 1;

const r = (i, s) => ({ op_id: "kl-" + i, status: s });
const scens = {};
// mixed terminal/pending over cap (terminal evicted oldest-first)
const mixed = [];
for (let i = 0; i < 10; i++) mixed.push(r(i, "committed"));
mixed.push(r(10, "pending"));
for (let i = 11; i < 14; i++) mixed.push(r(i, "committed"));
scens.mixed = { in: mixed, cap: 5 };
// all-pending over cap (oldest pending evicted, newest kept)
scens.allPending = {
  in: Array.from({ length: 7 }, (_, i) => r(i, "pending")), cap: 5 };
// pending DEPRIORITIZED: 1 oldest pending + 4 newer terminal, cap 4
const mixed2 = [r(0, "pending")];
for (let i = 1; i <= 4; i++) mixed2.push(r(i, "committed"));
scens.priority = { in: mixed2, cap: 4 };
for (const [name, s] of Object.entries(scens)) {
  out.receipts[name] = boundReceipts(s.in, s.cap).map((x) => x.op_id);
}
out.receipts.emptyIn = JSON.stringify(boundReceipts([], 5));
out.receipts.nonArrayIn = JSON.stringify(boundReceipts(null, 5));
out.receipts.zeroCap = JSON.stringify(boundReceipts(mixed, 0));
out.receipts.negCap = JSON.stringify(boundReceipts(mixed, -3));

const q = (sess, i) => ({ session: sess, kind: "failure",
  text: "entry-" + sess + "-" + i });
const qs = {};
// current retained preferentially, non-current evicted oldest-first
const cq = [q("cur", 1), q("cur", 2), q("cur", 3)];
for (let i = 0; i < 7; i++) cq.push(q("o" + i, 1));
qs.mixed = { in: cq, cap: 5, cur: "cur" };
// only-current over cap: oldest current evicted (recency window)
qs.currentOnly = {
  in: Array.from({ length: 7 }, (_, i) => q("cur", i)), cap: 5,
  cur: "cur" };
// non-current bound entirely (current session irrelevant here)
const cq3 = Array.from({ length: 6 }, (_, i) => q("o" + i, 1));
qs.nonCurrentOnly = { in: cq3, cap: 5, cur: "cur" };
for (const [name, s] of Object.entries(qs)) {
  out.queue[name] = boundCaptureQueue(s.in, s.cap, s.cur)
    .map((e) => e.session + ":" + e.text);
}
out.queue.emptyIn = JSON.stringify(boundCaptureQueue([], 5, "cur"));
out.queue.nonArrayIn = JSON.stringify(boundCaptureQueue(null, 5, "cur"));
out.queue.zeroCap = JSON.stringify(boundCaptureQueue(cq, 0, "cur"));
out.queue.negCap = JSON.stringify(boundCaptureQueue(cq, -1, "cur"));

process.stdout.write(JSON.stringify(out) + "\\n");
"""

# ------------------------------------------------------------- live probe

_LIVE_PROBE = """
import fs from "node:fs";
const out = { stateModule: false, caps: null };
let capR = 32, capQ = 32;   // base fallback keeps the live RED deterministic
try {
  const sm = await import(process.env.STATE_URL);
  if (sm.CAPS && Number.isInteger(sm.CAPS.receipts)) capR = sm.CAPS.receipts;
  if (sm.CAPS && Number.isInteger(sm.CAPS.captureQueue)) capQ = sm.CAPS.captureQueue;
  out.caps = { receipts: capR, captureQueue: capQ };
  out.stateModule = true;
} catch { /* base: state.mjs absent; drive on with fallback caps */ }

const handlers = {};
const registered = [];
const ctx = {
  on: (name, fn) => { (handlers[name] ??= []).push(fn); },
  tools: { register: (spec) => registered.push(spec) },
};
const mod = await import(process.env.PLUGIN_URL);
await mod.plugin(ctx);
const add = registered.find((t) => t && t.name === "zmem_add");
if (!add) throw new Error("zmem_add tool not registered");
const state = globalThis.__zmemDsPluginState;
if (!state) throw new Error("plugin state not exposed");

// -- receipts live drive: more receipts than the cap, few pending
//    (statuses from the stub's rotation, 2 receipts per op).
const calls = Math.max(Math.ceil((capR + 12) / 2), 8);
for (let i = 0; i < calls; i++) {
  const r = await add.execute({ namespace: "user:global",
    text: "receipt probe " + i });
  if (!r || r.isError) {
    throw new Error("zmem_add execute failed: "
      + JSON.stringify(r).slice(0, 200));
  }
}
const stub = JSON.parse(fs.readFileSync(process.env.STUB_STATE, "utf8"));
out.receipts = {
  cap: capR,
  fed: 2 * calls,
  pendingFed: 2 * Object.values(stub.ops)
    .filter((s) => s === "pending").length,
  length: state.receipts.length,
  pendingReceipts: state.pendingReceipts,
  array: state.receipts.map((x) => ({ op_id: x && x.op_id,
    status: x && x.status })),
};

// -- captureQueue live drive A: over-cap CURRENT-session backlog.
const prestep = handlers["agent/pre-step"][0];
const msg = (t) => ({ source: { kind: "user", captureEventId: "ce-" + t },
  content: [{ type: "text", text: t + " correction body" }] });
const runPre = async (sess, t) => prestep(
  { agent: { id: sess }, messages: [] },
  async () => ({ kind: "enter", messages: [msg(t)] }));
const currentFeed = Math.max(capQ + 8, 40);
for (let i = 0; i < currentFeed; i++) {
  await runPre("sess-current", "curmarker" + i);
}
const settle = async () => {
  let stable = 0, lastLen = -1;
  const deadline = Date.now() + 20000;
  while (stable < 3 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 150));
    if (state.captureQueue.length === lastLen) stable += 1;
    else { stable = 0; lastLen = state.captureQueue.length; }
  }
};
await settle();
out.currentBacklog = {
  cap: capQ,
  fed: currentFeed,
  length: state.captureQueue.length,
  array: state.captureQueue.map((e) => ({ session: e && e.session,
    text: e && e.text })),
};

// -- captureQueue live drive B: multi-session interleave under the
//    WRITE-TIME binding law (the last write binds the session).
state.captureQueue.length = 0;
const interFeed = [];
for (let i = 0; i < 5; i++) interFeed.push(["sess-cur2", "i-cur" + i]);
const others = capQ + 5;
for (let i = 0; i < others; i++) {
  interFeed.push(["sess-x" + (i % 5), "i-oth" + i]);
}
interFeed.push(["sess-cur2", "i-cur-final"]);
for (const [sess, t] of interFeed) await runPre(sess, t);
await settle();
out.interleave = {
  cap: capQ,
  fed: interFeed.length,
  length: state.captureQueue.length,
  array: state.captureQueue.map((e) => ({ session: e && e.session,
    text: e && e.text })),
};
process.stdout.write(JSON.stringify(out) + "\\n");
"""

# ----------------------------------------------------------- restore probe

_RESTORE_PROBE = """
import fs from "node:fs";
let cap = 32;
try {
  const sm = await import(process.env.STATE_URL);
  if (sm.CAPS && Number.isInteger(sm.CAPS.receipts)) cap = sm.CAPS.receipts;
} catch { /* base: fallback cap */ }
// An OLD, over-cap state file (the pre-fix unbounded shape), sized FROM
// the cap.
const staleLen = cap + 5;
const staleReceipts = Array.from({ length: staleLen }, (_, i) =>
  ({ op_id: "kl-old-" + i, status: "committed" }));
fs.writeFileSync(process.env.ZMEM_DEEPSEEK_STATE,
  JSON.stringify({ schema: "zmem-deepseek-state-v1", sessions: {},
    captureQueue: [], receipts: staleReceipts, pendingReceipts: 0 }));
const handlers = {};
const registered = [];
const ctx = {
  on: (name, fn) => { (handlers[name] ??= []).push(fn); },
  tools: { register: (spec) => registered.push(spec) },
};
const mod = await import(process.env.PLUGIN_URL);
await mod.plugin(ctx);
const add = registered.find((t) => t && t.name === "zmem_add");
// Enough NEW receipts to exceed the cap on their own: the next write
// must bound whatever it writes.
const calls = Math.max(Math.ceil((cap + 8) / 2), 6);
for (let i = 0; i < calls; i++) {
  await add.execute({ namespace: "user:global", text: "restore probe " + i });
}
const after = JSON.parse(
  fs.readFileSync(process.env.ZMEM_DEEPSEEK_STATE, "utf8"));
process.stdout.write(JSON.stringify({
  cap,
  staleLen,
  fileLen: (after.receipts || []).length,
  array: (after.receipts || []).map((x) => ({ op_id: x && x.op_id,
    status: x && x.status })),
}) + "\\n");
"""


# ------------------------------------------------------------ phantom probe

_PHANTOM_PROBE = """
let capR = 32;
try {
  const sm = await import(process.env.STATE_URL);
  if (sm.CAPS && Number.isInteger(sm.CAPS.receipts)) capR = sm.CAPS.receipts;
} catch { /* base: fallback cap */ }
const handlers = {};
const registered = [];
const ctx = {
  on: (name, fn) => { (handlers[name] ??= []).push(fn); },
  tools: { register: (spec) => registered.push(spec) },
};
const mod = await import(process.env.PLUGIN_URL);
await mod.plugin(ctx);
const add = registered.find((t) => t && t.name === "zmem_add");
if (!add) throw new Error("zmem_add tool not registered");
const state = globalThis.__zmemDsPluginState;
if (!state) throw new Error("plugin state not exposed");
// FB-008 phantom-pending leg: every op feeds the ACTUAL event
// sequence — the enqueue receipt PENDING, then the flush receipt
// COMMITTED, for the SAME op_id (stub knobs). The terminal flush
// receipt must RETIRE its own stale pending twin, so the bounded
// array holds only committed receipts and pendingReceipts is 0.
const calls = capR + 5;
for (let i = 0; i < calls; i++) {
  const r = await add.execute({ namespace: "user:global",
    text: "phantom probe " + i });
  if (!r || r.isError) {
    throw new Error("zmem_add execute failed: "
      + JSON.stringify(r).slice(0, 200));
  }
}
process.stdout.write(JSON.stringify({
  cap: capR,
  fed: calls,
  fedEvents: 2 * calls,
  length: state.receipts.length,
  pendingReceipts: state.pendingReceipts,
  array: state.receipts.map((x) => ({ op_id: x && x.op_id,
    status: x && x.status })),
}) + "\\n");
"""


def _node_available() -> bool:
    try:
        proc = subprocess.run(["node", "--version"], capture_output=True,
                              text=True, timeout=15)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _python_on_path() -> bool:
    import shutil
    return shutil.which("python") is not None


class StateBoundsTest(unittest.TestCase):
    def setUp(self) -> None:
        if not (_node_available() and _python_on_path()):
            self.skipTest("node/python not on PATH for the state-bounds legs")
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-dsprobe-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def _write_tree(self) -> None:
        tmp = self.tmp
        for rel, text in (
            ("dshstub/index.mjs", _STUB_DSH_LLM),
            ("reg/stub-resolve.mjs", _RESOLVE_HOOK),
            ("reg/reghook.mjs", _REGISTER_HOOK),
            ("root/skills/memory/scripts/store.py", _STUB_STORE_PY),
            ("root/skills/memory/scripts/knowledge/policy.py",
             "import sys\nsys.exit(0)\n"),
            ("root/hooks/lib/zmem-capture-entry.py", _STUB_CAPTURE_ENTRY),
        ):
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        (tmp / "stub-state.json").write_text(
            '{"next": 1, "ops": {}}', encoding="utf-8")

    def _env(self) -> dict:
        tmp = self.tmp
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")
               and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                             "NODE_OPTIONS")}
        env.update({
            "PLUGIN_URL": INDEX_MJS.as_uri(),
            "STATE_URL": STATE_MJS.as_uri(),
            "DSH_LLM_STUB_URL": (tmp / "dshstub" / "index.mjs").as_uri(),
            "STUB_STATE": str(tmp / "stub-state.json"),
            "ZMEM_DEEPSEEK_STATE": str(tmp / "state.json"),
            "ZMEM_ROOT": str(tmp / "root"),
            "ZMEM_STORE": str(tmp / "store.sqlite"),
            "ZMEM_DATA": str(tmp),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "NODE_OPTIONS": "--import=" + (tmp / "reg" / "reghook.mjs").as_uri(),
            "PYTHONIOENCODING": "utf-8",
        })
        return env

    def _run_node(self, script: str, timeout: float = 180.0,
                  env: dict | None = None) -> dict:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env or self._env(), cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL, timeout=timeout)
        self.assertEqual(
            proc.returncode, 0,
            "node probe failed rc=%d: %r" % (proc.returncode,
                                             proc.stderr[-600:]))
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return json.loads(lines[-1])

    def test_arrays_bounded_per_ledger_mechanism(self):
        # (1) LIVE leg: the REAL index.mjs arrays equal the oracle
        # simulated over the SAME chronological feed.
        self._write_tree()
        live = self._run_node(_LIVE_PROBE)

        # -- receipts: the stub's rotation is deterministic (n%4==0
        #    pending; two receipts per op, in order).
        cap_r = live["receipts"]["cap"]
        calls = max(-(-((cap_r + 12) // 2)), 8)
        feed = []
        for n in range(1, calls + 1):
            status = "pending" if n % 4 == 0 else "committed"
            feed.extend([status, status])
        oracle = _oracle_receipts(
            [{"op_id": None, "status": s} for s in feed], cap_r)
        self.assertEqual(live["receipts"]["fed"], len(feed))
        self.assertEqual(
            live["receipts"]["length"], len(oracle),
            "state.receipts length must equal the oracle (fed=%d cap=%d "
            "oracle=%d live=%d — at base onReceipt pushes unbounded)"
            % (len(feed), cap_r, len(oracle), live["receipts"]["length"]))
        self.assertEqual(_statuses(live["receipts"]["array"]),
                         _statuses(oracle),
                         "the retained receipts equal the oracle "
                         "(status sequence)")
        self.assertEqual(
            live["receipts"]["pendingReceipts"],
            _statuses(oracle).count("pending"),
            "pendingReceipts is recomputed from the BOUNDED array")

        # -- current-session backlog: bounded per the oracle.
        cap_q = live["currentBacklog"]["cap"]
        current_feed = max(cap_q + 8, 40)
        oracle_q = _oracle_push_sequence(
            [("sess-current", "curmarker%d" % i)
             for i in range(current_feed)], cap_q, "queue")
        self.assertEqual(
            live["currentBacklog"]["length"], len(oracle_q),
            "the over-cap CURRENT-session backlog is bounded (oracle=%d "
            "live=%d cap=%d)" % (len(oracle_q),
                                 live["currentBacklog"]["length"], cap_q))
        self.assertEqual(
            _markers(live["currentBacklog"]["array"]),
            _markers(oracle_q),
            "the retained backlog equals the oracle (recency window)")
        # eligibility: the newest current-session entry survives.
        self.assertIn("curmarker%d" % (current_feed - 1),
                      _markers(oracle_q))

        # -- interleave under the WRITE-TIME binding law.
        inter_feed = [("sess-cur2", "i-cur%d" % i) for i in range(5)]
        inter_feed += [("sess-x%d" % (i % 5), "i-oth%d" % i)
                       for i in range(cap_q + 5)]
        inter_feed.append(("sess-cur2", "i-cur-final"))
        oracle_i = _oracle_push_sequence(inter_feed, cap_q, "queue")
        self.assertEqual(
            live["interleave"]["length"], len(oracle_i),
            "the interleaved queue equals the oracle's length (oracle=%d "
            "live=%d cap=%d)" % (len(oracle_i),
                                 live["interleave"]["length"], cap_q))
        self.assertEqual(
            _markers(live["interleave"]["array"]), _markers(oracle_i),
            "the retained interleave equals the oracle (push-time "
            "session binding)")
        # eligibility disclosure: the LAST-ACTIVE session still has an
        # entry inside the retained window (recency-window contract).
        self.assertTrue(
            any(e["session"] == "sess-cur2" for e in oracle_i),
            "the last-active session keeps >=1 entry in the retained "
            "window (the nudge-eligibility signal)")

        # (2) RESTORE leg: an over-cap OLD state file (sized FROM the
        # cap by the probe) plus a fresh over-cap drive — the next write
        # must bound whatever it writes, per the oracle.
        self._write_tree()
        restore = self._run_node(_RESTORE_PROBE)
        cap = restore["cap"]
        calls_r = max(-(-((cap + 8) // 2)), 6)
        feed_r = []
        for n in range(1, calls_r + 1):
            status = "pending" if n % 4 == 0 else "committed"
            feed_r.extend([status, status])
        oracle_r = _oracle_receipts(
            [{"op_id": None, "status": s} for s in feed_r], cap)
        self.assertGreaterEqual(restore["staleLen"], cap + 5)
        self.assertEqual(
            restore["fileLen"], len(oracle_r),
            "the next write bounds the receipts array per the oracle "
            "(staleLen=%d fed=%d oracle=%d fileLen=%d cap=%d)"
            % (restore["staleLen"], len(feed_r), len(oracle_r),
               restore["fileLen"], cap))
        self.assertEqual(_statuses(restore["array"]),
                         _statuses(oracle_r))

        # (2b) PHANTOM leg (FB-008): >= cap local ops feeding the
        # ACTUAL two-event sequence per op_id — the enqueue receipt
        # ALWAYS pending, the flush receipt COMMITTED (stub knobs).
        # The terminal flush receipt must RETIRE its own stale pending
        # twin, so the bounded array holds committed receipts only and
        # pendingReceipts is 0 (no-dedupe mutant: the stale twins
        # linger as PHANTOM pending entries behind their own committed
        # outcomes). The oracle runs over the post-retire feed — ONE
        # final (committed) receipt per op — under the same frozen
        # boundReceipts law.
        self._write_tree()
        phantom = self._run_node(_PHANTOM_PROBE, env={
            **self._env(),
            "STUB_ENQUEUE_STATUS": "pending",
            "STUB_FLUSH_COMMIT": "1",
        })
        cap_p = phantom["cap"]
        calls_p = cap_p + 5
        oracle_p = _oracle_receipts(
            [{"op_id": None, "status": "committed"}
             for _ in range(calls_p)], cap_p)
        self.assertEqual(phantom["fed"], calls_p)
        self.assertEqual(
            phantom["fedEvents"], 2 * calls_p,
            "the phantom leg drives the ACTUAL event feed: one pending "
            "and one committed receipt per op through the real "
            "enqueue+flush path")
        self.assertEqual(
            phantom["length"], len(oracle_p),
            "the phantom drive is bounded per the oracle (fed=%d "
            "oracle=%d live=%d cap=%d)"
            % (calls_p, len(oracle_p), phantom["length"], cap_p))
        self.assertEqual(_statuses(phantom["array"]),
                         _statuses(oracle_p),
                         "only COMMITTED receipts survive the phantom "
                         "retire (status sequence)")
        self.assertEqual(
            phantom["pendingReceipts"], 0,
            "no PHANTOM pending entries survive the commit "
            "(pendingReceipts recomputed from the retired bounded "
            "array)")


        # (3) PURE leg: state.mjs equals the INDEPENDENT ORACLE for every
        # scenario (never hand-counted).
        self.assertTrue(
            STATE_MJS.is_file(),
            "deepseek-plugin/state.mjs must exist (pure bounding module)")
        pure = self._run_node(_PURE_PROBE)

        # -- receipts scenarios vs the oracle
        mixed = ([{"op_id": "kl-%d" % i, "status": "committed"}
                  for i in range(10)]
                 + [{"op_id": "kl-10", "status": "pending"}]
                 + [{"op_id": "kl-%d" % i, "status": "committed"}
                    for i in range(11, 14)])
        all_pending = [{"op_id": "kl-%d" % i, "status": "pending"}
                       for i in range(7)]
        priority = ([{"op_id": "kl-0", "status": "pending"}]
                    + [{"op_id": "kl-%d" % i, "status": "committed"}
                       for i in range(1, 5)])
        for name, feed, cap in (("mixed", mixed, 5),
                                ("allPending", all_pending, 5),
                                ("priority", priority, 4)):
            expected = [e["op_id"]
                        for e in _oracle_receipts(feed, cap)]
            self.assertEqual(
                pure["receipts"][name], expected,
                "boundReceipts(%s) must equal the independent oracle "
                "(got=%r oracle=%r)" % (name, pure["receipts"][name],
                                        expected))
        self.assertEqual(pure["receipts"]["emptyIn"], "[]")
        self.assertEqual(pure["receipts"]["nonArrayIn"], "[]")
        self.assertEqual(pure["receipts"]["zeroCap"], "[]")
        self.assertEqual(pure["receipts"]["negCap"], "[]")

        # -- queue scenarios vs the oracle
        def q(sess, i):
            return {"session": sess, "kind": "failure",
                    "text": "entry-%s-%d" % (sess, i)}
        q_mixed = ([q("cur", i) for i in (1, 2, 3)]
                   + [q("o%d" % i, 1) for i in range(7)])
        q_current = [q("cur", i) for i in range(7)]
        q_noncur = [q("o%d" % i, 1) for i in range(6)]
        for name, feed, cap, cur in (("mixed", q_mixed, 5, "cur"),
                                     ("currentOnly", q_current, 5, "cur"),
                                     ("nonCurrentOnly", q_noncur, 5, "cur")):
            expected = ["%s:%s" % (e["session"], e["text"])
                        for e in _oracle_queue(feed, cap, cur)]
            self.assertEqual(
                pure["queue"][name], expected,
                "boundCaptureQueue(%s) must equal the independent oracle "
                "(got=%r oracle=%r)" % (name, pure["queue"][name],
                                        expected))
        # eligibility disclosure: a current-session entry INSIDE the
        # retained window survives (the recency-window contract).
        self.assertTrue(any(e["session"] == "cur"
                            for e in _oracle_queue(q_current, 5, "cur")))
        self.assertEqual(pure["queue"]["emptyIn"], "[]")
        self.assertEqual(pure["queue"]["nonArrayIn"], "[]")
        self.assertEqual(pure["queue"]["zeroCap"], "[]")
        self.assertEqual(pure["queue"]["negCap"], "[]")

if __name__ == "__main__":
    unittest.main()
