// ZMem native DeepSeek (dsh/Cordis) adapter — plugin entry (issues #374/#375).
// Wires the pinned 0.2.1-rc.2 event surface (see docs/plans reference and
// tests/fixtures/deepseek/scenario.mjs): first-accepted-pre-step delivery
// with reserve-then-admit-then-ack, tools/result capture, turn-stopping
// nudge, compaction serialization, dispose drain. Fail-open everywhere.
import fs from "node:fs";
import path from "node:path";
import { tmpdir } from "node:os";
const pathJoin = (...a) => a.join(path.sep).split(/[\/]+/).join(path.sep);
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { identity, ledgerKey } from "./identity.mjs";
import * as backend from "./launcher.mjs";
import { registerTools, TOOL_SPECS } from "./tools.mjs";
import { classifyUserCorrection, classifyToolOutcome } from "./capture.mjs";

const STATE_SCHEMA = "zmem-deepseek-state-v1";
const COMPACTION_EVENTS = new Set(["compaction/start", "compaction/end",
  "compaction/prune", "compaction/summary"]);
const SOURCE_KIND = "zmem";
const CAPTURE_OFF = () =>
  (process.env.ZMEM_CAPTURE || "") === "0" ||
  (process.env.ZMEM_INJECT || "") === "0";
// Kill-switch parity: ZMEM_CAPTURE gates capture/state-mutation only;
// passive recall delivery gates on ZMEM_INJECT alone, so an operator
// disabling capture does not silently disable recall.
const INJECT_OFF = () => (process.env.ZMEM_INJECT || "") === "0";

function statePath() {
  return process.env.ZMEM_DEEPSEEK_STATE || "";
}

function loadState() {
  if (!statePath()) return null;
  try { return JSON.parse(fs.readFileSync(statePath(), "utf8")); }
  catch { return null; }
}

export function currentState() { return loadState(); }

export function plugin(ctx, config = {}) {
  // In-flight capture admissions: turn-stopping drains these (bounded by
  // this process's own spawns) before deciding nudge eligibility, so the
  // decision never races the async capture pipeline.
  const pendingCaptures = new Set();
  const trackCapture = (promise) => {
    pendingCaptures.add(promise);
    Promise.resolve(promise).finally(() => pendingCaptures.delete(promise))
      .catch(() => {});
    return promise;
  };
  const state = {
    schema: STATE_SCHEMA,
    sessions: {},
    captureQueue: [],
    receipts: [],
    autoRetainEffective: false,
    closeoutReceipt: null,
    persistsAcrossDispose: true,
    secondStoreCreated: false,
    // top-level projection of the most recent session (the observers'
    // accessors; per-session entries are authoritative)
    deliveredText: "", reserved: false, reservationId: null, acked: false,
    backendMode: "local", storePathUsed: null, envelopeKeys: [],
    pendingReceipts: 0, compactionResets: 0, deadlineApplied: false,
    bounded: false, guidance: "next-request-gap", reason: "",
    nudgeCount: 0, nudgeBound: 1, selfTriggeredContinuation: false,
    ledgerKey: "", ledgerKeyDistinct: true,
  };
  const writeState = () => {
    if (!statePath()) return;
    try {
      fs.writeFileSync(statePath(), JSON.stringify(state, null, 1));
    } catch { /* fail-open observability */ }
  };
  // Effective auto-retain (issue #375 AC2), mirroring retain.py's
  // _retention_enabled + _config_gate_value semantics: exact env "0"/"1"
  // wins; unset falls to the store-side <data>/config.json auto_retain
  // (strict boolean, missing -> false, non-bool -> false with the value
  // never escalated). Derived, never self-reported from nothing.
  const resolveAutoRetain = () => {
    const env = (process.env.ZMEM_AUTO_RETAIN || "").trim();
    if (env === "1") return true;
    if (env === "0" || env) return env === "1";
    try {
      const dataDir = process.env.ZMEM_DATA
        || process.env.ZCODE_PLUGIN_DATA
        || process.env.CLAUDE_PLUGIN_DATA || "";
      if (!dataDir) return false;
      const cfg = JSON.parse(fs.readFileSync(
        pathJoin(dataDir, "config.json"), "utf8"));
      return cfg.auto_retain === true;
    } catch {
      return false;
    }
  };
  state.autoRetainEffective = resolveAutoRetain();
  globalThis.__zmemDsPluginState = state;
  globalThis.__zmemDsWriteState = writeState;

  const sessionOf = (agent) => {
    const ident = identity(agent);
    if (!state.sessions[ident.session]) {
      state.sessions[ident.session] = {
        namespace: ident.namespace, lane: "deepseek",
        ledgerKey: ledgerKey(ident), acked: false, deliveredText: "",
        reserved: false, reservationId: null, ledgerKeyDistinct: true,
      };
    }
    return { ident, entry: state.sessions[ident.session] };
  };

  const project = (ident, entry) => {
    state.ledgerKey = entry.ledgerKey;
    state.ledgerKeyDistinct =
      Object.values(state.sessions).filter(
        (s) => s.ledgerKey === entry.ledgerKey).length === 1;
    state.backendMode =
      (process.env.ZMEM_REMOTE_URL || "").trim() ? "remote" : "local";
    state.storePathUsed =
      process.env.ZMEM_STORE || (state.backendMode === "local"
        ? `${process.env.ZMEM_DATA || ""}/store.sqlite` : null);
  };

  ctx.on("agent/created", async (payload) => {
    if (CAPTURE_OFF()) return;
    try {
      const { ident, entry } = sessionOf(payload.agent);
      // Fresh-process epoch (issue #374 AC1 retry leg): a NEW adapter
      // process for a session re-primes context like the hook hosts'
      // SessionStart does — clear the session's delivery ledger through
      // the EXISTING backend verb so the anti-repeat guarantee stays
      // within one process lifetime, and a rerun delivers exactly once
      // more with an idempotent ack.
      // LOCAL mode only: in remote mode this ALLOW-LOCAL command would
      // CONNECT a local store (the no-split-brain violation) — the
      // delivery ledger for remote sessions lives hub-side.
      if (!(process.env.ZMEM_REMOTE_URL || "").trim()) {
        await backend.runTool(["ledger-clear", "--session-id",
          ident.session,
          ...(ident.principal
            ? ["--principal", ident.principal, "--harness", "deepseek"]
            : [])]);
      }
      await backend.runMoment("session_start", {
        session: ident.session, namespace: ident.namespace, cwd: ident.cwd });
      project(ident, entry);
      writeState();
    } catch { /* fail-open */ }
  });

  ctx.on("agent/pre-step", async ({ agent, messages, signal }, next) => {
    const decision = await next();
    if (!decision || decision.kind !== "enter") {
      return decision; // rejection/cancellation passes through untouched
    }
    // Handler gate = injection parity: recall delivery below must not be
    // suppressed by the CAPTURE kill switch (only INJECT=0 does that).
    // Correction capture inside the loop keeps its own CAPTURE gate.
    if (INJECT_OFF()) return decision;
    // Original-user-input correction capture (issue #375 AC1; capability
    // matrix row correction-capture: "original user messages at accepted
    // pre-step"): the shared python entry owns gates/redaction/dedup;
    // plugin-, tool- and replay-sourced text is filtered by the source
    // kind here and by the python gates there.
    for (const m of (decision.messages || [])) {
      if (!CAPTURE_OFF() && classifyUserCorrection(m)) {
        const ctext = (Array.isArray(m.content) ? m.content : [])
          .map((b) => (b && b.type === "text" ? b.text || "" : "")).join(" ");
        if (ctext && ctext.length >= 5) {
          const sid = String(agent?.id || "");
          trackCapture(backend.runCapture("correction", ctext, {
            session: sid,
            namespace: (process.env.ZMEM_NAMESPACE || "user:global"),
            event_id: (m.source.captureEventId || null),
          })).then((r) => {
            if (r.result && r.result.ok && !r.result.deduped) {
              state.captureQueue.push({
                kind: "correction", redacted: true,
                deduped: Boolean(r.result.deduped),
                text: String(r.result.text || ctext.slice(0, 120)),
                host: "deepseek", session: sid,
                namespace: (process.env.ZMEM_NAMESPACE || "user:global"),
              });
              writeState();
            }
          }).catch(() => { /* fail-open */ });
        }
      }
    }
    let ident, entry;
    try {
      ({ ident, entry } = sessionOf(agent));
    } catch { return decision; }
    if (INJECT_OFF()) return decision;
    try {
      const query = (messages || [])
        .filter((m) => m?.source?.kind === "user")
        .map((m) => m?.content?.map?.((b) => b?.text || "")
          .join(" ") || "")
        .join(" ").slice(0, 400);
      const moment = await backend.runMoment("user_prompt", {
        query, session: ident.session, namespace: ident.namespace,
        cwd: ident.cwd });
      const envelope = moment.envelope;
      if (!envelope) {
        state.reason = "no-envelope: code=%s out=%s err=%s".replace(
          "code=%s", "code=" + moment.raw.code)
          .replace("out=%s", "out=" + (moment.raw.out || "").slice(0, 200))
          .replace("err=%s", "err=" + (moment.raw.err || "").slice(0, 300));
      }
      const rendered = envelope && envelope.rendered;
      state.envelopeKeys = envelope ? Object.keys(envelope).sort() : [];
      state.deadlineApplied = true;
      if (!rendered) {
        if (envelope) state.reason = envelope.reason || "empty";
        project(ident, entry); writeState();
        return decision;
      }
      state.reserved = Boolean(envelope.reservation);
      state.reservationId = envelope.reservation || null;
      entry.reserved = state.reserved;
      entry.reservationId = state.reservationId;
      // Bounded payload: cap the delivered text (declare the bound).
      const bound = 6000;
      const text = rendered.length > bound
        ? rendered.slice(0, bound) : rendered;
      state.bounded = rendered.length > bound;
      state.deliveredText = text;
      state.acked = false;
      entry.deliveredText = text;
      entry.acked = false;
      state.pendingMsgId = null;
      const msg = createUserMessage({
        content: [{ type: "text", text }],
        source: { kind: SOURCE_KIND, form: "recall" },
      });
      state.pendingMsgId = msg.id;
      state.pendingIdent = ident.session;
      project(ident, entry);
      writeState();
      // Spread the original decision — startsRequestSeries and every other
      // field survive the append (README contract; frozen series probe).
      return { ...decision, messages: [...decision.messages, msg] };
    } catch {
      state.reason = "fail-open";
      writeState();
      return decision;
    }
  });

  ctx.on("session/event", (session, event) => {
    const type = String(event?.type || "");
    // Admission observation -> acknowledge the reservation NOW (and only
    // now): OUR appended zmem message reached the durable session log —
    // matched by the zmem source kind (the user's own messages carry
    // kind "user"; the recall message carries SOURCE_KIND), or by exact
    // message id when the host preserves it. Matching only the session
    // let any later user/message event commit a recall that was never
    // admitted (PR #409 review, #374 AC1).
    const evData = (event && event.data) || {};
    const evMsg = (evData.message && typeof evData.message === "object")
      ? evData.message : ((event && event.message) || null);
    const evMsgId = String(evMsg?.id ?? event?.id ?? "");
    const evKind = String(evMsg?.source?.kind ?? evData?.source?.kind ?? "");
    if (type === "user/message" && state.pendingMsgId &&
        String(session?.id || "") === String(state.pendingIdent) &&
        (evKind === SOURCE_KIND ||
         (!!evMsgId && evMsgId === String(state.pendingMsgId)))) {
      const entry = state.sessions[state.pendingIdent];
      const reservation = state.reservationId;
      state.pendingMsgId = null;
      if (reservation) {
        // Bounded SYNCHRONOUS ack at the admission moment: the durable
        // delivery-ledger write must land before the runtime can dispose
        // (emit-mode listeners' async tails are not awaited). acked is
        // set from the ack RESULT, not before it.
        try {
          // acked only on an explicit backend success: ackReservationSync
          // reports { ok: false } for nonzero/timeout spawns without
          // throwing, and a false-admitted recall suppresses the
          // reservation for the rest of the session.
          const ack = backend.ackReservationSync(reservation);
          if (ack && ack.ok === true) {
            state.acked = true;
            if (entry) entry.acked = true;
          }
        } catch { /* fail-open: stay unacked, never false-admitted */ }
      } else {
        state.acked = false;
      }
      writeState();
      return;
    }
    if (COMPACTION_EVENTS.has(type) && type === "compaction/end") {
      // Serialize the in-memory admission/ledger view reset before the
      // next accepted request; persisted state is NEVER cleared.
      state.compactionResets += 1;
      state.deliveredText = "";
      state.reserved = false;
      state.reservationId = null;
      writeState();
    }
  });

  ctx.on("tools/pre-execute", async (exec, next) => {
    // Context collection only: the in-flight call cannot be changed (the
    // declared capability gap; guidance rides the NEXT accepted pre-step).
    state.guidance = "next-request-gap";
    return next();
  });

  ctx.on("tools/result", (exec, result) => {
    if (CAPTURE_OFF() || !classifyToolOutcome(exec, result)) return;
    const text = String(result.error?.message || "").slice(0, 2000);
    if (!text) return;
    trackCapture(backend.runCapture("failure", text, {
      session: String(exec?.agent?.id || ""),
      namespace: (process.env.ZMEM_NAMESPACE || "user:global"),
    })).then((r) => {
      if (r.result && r.result.ok) {
        state.captureQueue.push({
          kind: "failure", redacted: true, deduped: Boolean(r.result.deduped),
          text: text.slice(0, 120),
        });
        writeState();
      }
    }).catch(() => { /* fail-open */ });
  });

  ctx.on("agent/turn-stopping", async (payload) => {
    // Bounded reflection nudge (issue #375 AC3). Eligibility predicate =
    // THIS process captured eligible review evidence (a non-deduped
    // correction or failure for a session); idempotency = an O_EXCL
    // once-per-session marker next to the delivery-ledger sidecars (the
    // #398 closeout-reminder pattern); the bound is fixed at 1. The nudge
    // is surfaced as state for the HOST/closeout surface — the plugin
    // NEVER issues a self-triggered followup/inject (no recursion by
    // construction).
    if (CAPTURE_OFF()) return;
    if (state.nudgeCount >= state.nudgeBound) return;
    try {
      // Drain this process's in-flight captures first: eligibility must
      // observe the turn's own admissions, not a mid-flight pipeline.
      await Promise.allSettled([...pendingCaptures]);
      const { ident } = sessionOf(payload.agent);
      const eligible = state.captureQueue.some(
        (e) => e && e.session === ident.session && !e.deduped
          && (e.kind === "correction" || e.kind === "failure"));
      if (!eligible) {
        writeState();
        return;
      }
      // The shared reflect hook runs FIRST within the same bound: its own
      // eligibility (source_exists) and signal-change idempotency govern
      // whether a prompt is emitted; the plugin surfaces the outcome as
      // state and NEVER issues a self-triggered followup. The
      // once-per-session marker below still caps the nudge count.
      // reflectHookRan is the invocation-time observable (final-critic
      // finding 1): set the moment the hook subprocess is invoked — a
      // wiring-removal mutant leaves it unset and C10 fails. The hook's
      // exit status is fail-open by contract and not part of the claim.
      state.reflectHookRan = true;
      try {
        await backend.runReflect(ident.session, ident.namespace);
      } catch { /* fail-open */ }
      let once = false;
      // Host-managed tmpdir, never the process CWD: with no plugin-data
      // env var set the marker must still land somewhere writable without
      // creating ./ops in whatever directory the harness launched from.
      const marker = pathJoin(
        (process.env.ZMEM_DATA || process.env.ZCODE_PLUGIN_DATA
          || process.env.CLAUDE_PLUGIN_DATA || tmpdir()), "ops",
        ("nudged-" + ident.session).slice(0, 80));
      try {
        fs.mkdirSync(pathJoin(marker, ".."), { recursive: true });
        const fd = fs.openSync(marker, "wx");
        fs.closeSync(fd);
        once = true;
      } catch {
        // Existing marker (or a failed probe): this session was already
        // nudged — report the bound as reached so the observable state is
        // truthful instead of showing an uncounted nudge.
        once = false;
      }
      state.nudgeCount = once
        ? state.nudgeCount + 1
        : state.nudgeBound;
      state.selfTriggeredContinuation = false;
      writeState();
    } catch { /* fail-open */ }
  });

  ctx.on("agent/disposed", async () => {
    // Bounded drain of owned pending work (the delivery-ledger ack): the
    // durable write lands before the runtime finishes disposing. Ordinary
    // disposal is NOT persisted-session termination: per-session entries
    // persist.
    state.persistsAcrossDispose = true;
    writeState();
  });

  try {
    registerTools(ctx, {
      onToolsError: (err) => {
        state.toolsError = String((err && err.message) || err).slice(0, 300);
        writeState();
      },
      onReceipt: (receipt) => {
        state.receipts.push(receipt);
        state.pendingReceipts = state.receipts.filter(
          (x) => x.status === "pending").length;
        writeState();
      },
      onCloseout: (receipt) => {
        state.closeoutReceipt = receipt;
        writeState();
      },
    });
  } catch (err) {
    state.toolsError = String((err && err.message) || err).slice(0, 300);
    writeState();
  }
}

// Cordis dependency injection: ctx.tools (dsh ToolRuntime) requires an
// explicit inject declaration on the plugin (bare ctx access throws
// 'cannot get property "tools" without inject').
plugin.inject = ["tools"];

export { TOOL_SPECS };
export default plugin;
