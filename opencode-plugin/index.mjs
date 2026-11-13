// ZMem native OpenCode adapter — plugin entry (issue #66).
//
// Runtime wiring uses the LIVE @opencode-ai/plugin hook surface
// (experimental.chat.system.transform, experimental.session.compacting,
// event, and tool registration via the exported tool() helper). The
// historical probe fixtures under tests/fixtures/opencode/ pin the
// SELECTION mechanism (probe-v1 selected / probe-v2 rejected) with the
// 2026-09-10 probe vocabulary; this module maps that selection onto the
// current SDK names so every registered hook is invokable by the real
// host (no dead registrations).
//
// Store boundary: every store touch goes through ONE python subprocess per
// injection — skills/memory/scripts/opencode_bridge.py — which owns the
// #158 selector call. This module never imports storelib, opens SQLite,
// reads the delivery ledger, or writes the correction queue.
//
// Fail-open everywhere: a bridge failure answers the empty envelope and
// never throws into OpenCode. An unknown-session drive (no callback
// sessionID, no buffered event session) skips the bridge entirely and
// answers the empty envelope WITHOUT a delivery-ledger entry — sessions
// must never share an injection identity (plan F2).

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pathToFileURL } from "node:url";
import { tool } from "@opencode-ai/plugin";

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.dirname(PLUGIN_DIR);
const BRIDGE = path.join(REPO_ROOT, "skills", "memory", "scripts",
                         "opencode_bridge.py");
const PYTHON = process.platform === "win32" ? "python" : "python3";

// Pending-event buffer: the event observer stores at most this many events
// per session (oldest dropped) and the next system transform drains them
// exactly once (issue #66 design step 4).
export const OPENCODE_PENDING_EVENT_CAP = 32;

const ENVELOPE_REQUIRED_KEYS = ["results", "count", "omitted", "reason",
  "excluded", "candidate_ids", "tokens_used", "tokens_budget",
  "budget_dropped", "budget_admission", "budget_truncated",
  "budget_dropped_protected", "arms", "rendered"];

const EMPTY_ENVELOPE_REASON = "bridge-error";

function emptyEnvelope(budget = 1500, reason = EMPTY_ENVELOPE_REASON) {
  const env = {
    results: [], count: 0, omitted: 0, reason, excluded: [],
    candidate_ids: [], tokens_used: 0, tokens_budget: budget,
    budget_dropped: 0, budget_admission: 0, budget_truncated: 0,
    budget_dropped_protected: 0, arms: {}, rendered: "",
  };
  return env;
}

function isEnvelope(value) {
  return value !== null && typeof value === "object"
    && ENVELOPE_REQUIRED_KEYS.every((k) => k in value);
}

// --- Canonical env (issue #66 design step 3) --------------------------------

export function buildOpenCodeEnv(input) {
  const storeDirectory = (input && input.storeDirectory)
    || path.join(homeDir(), ".zmem");
  const worktree = (input && input.worktree) || "";
  const sessionId = (input && input.sessionId) || "";
  const dataDir = expandHome(storeDirectory);
  const delimiter = path.delimiter;
  const home = homeDir();
  return {
    ZMEM_HOST: "opencode",
    ZMEM_TIER0: "native",
    ZMEM_DATA: dataDir,
    ZMEM_PROJECT: worktree,
    ZMEM_SESSION: sessionId,
    ZMEM_NAMESPACE: process.env.ZMEM_NAMESPACE || "",
    ZMEM_OPENCODE_TRANSCRIPTS:
      process.env.ZMEM_OPENCODE_TRANSCRIPTS
      || path.join(dataDir, "transcripts", "opencode"),
    ZMEM_SKILLS_DIRS: process.env.ZMEM_SKILLS_DIRS || [
      path.join(home, ".codex", "skills"),
      path.join(home, ".claude", "skills"),
      path.join(home, ".zcode", "skills"),
      path.join(home, ".config", "opencode", "skills"),
    ].join(delimiter),
  };
}

function homeDir() {
  return process.env.USERPROFILE || process.env.HOME || "";
}

function expandHome(p) {
  if (!p) return p;
  if (p === "~") return homeDir();
  if (p.startsWith("~/") || p.startsWith("~\\")) {
    return path.join(homeDir(), p.slice(2));
  }
  return p;
}

// --- Injection bridge (ONE python subprocess per request) --------------------

async function callBridge(request) {
  return await new Promise((resolve) => {
    const child = spawn(PYTHON, [BRIDGE], {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });
    let out = "";
    let settled = false;
    const finish = (value) => {
      if (!settled) { settled = true; resolve(value); }
    };
    child.stdout.on("data", (chunk) => { out += chunk.toString("utf-8"); });
    child.on("error", () => finish(emptyEnvelope(request.budget_tokens || 1500)));
    child.on("close", () => {
      try {
        const parsed = JSON.parse(out);
        if (isEnvelope(parsed)) {
          if (!Array.isArray(parsed.excluded)) parsed.excluded = [];
          finish(parsed);
        } else {
          finish(emptyEnvelope(request.budget_tokens || 1500));
        }
      } catch {
        finish(emptyEnvelope(request.budget_tokens || 1500));
      }
    });
    child.stdin.write(JSON.stringify(request), "utf-8");
    child.stdin.end();
  });
}

export async function runInjection(request) {
  const req = request || {};
  if (!req.session_id) {
    // Unknown session: fail open WITHOUT a ledger entry (plan F2) — no
    // bridge call, no shared injection identity.
    return emptyEnvelope(req.budget_tokens || 1500, "no-session");
  }
  return await callBridge(req);
}

// --- Pending event queue -----------------------------------------------------

const pending = new Map(); // sessionId -> array of events (cap per session)

export function drainPending(sessionId) {
  const drained = pending.get(sessionId) || [];
  pending.delete(sessionId);
  return drained;
}

function bufferEvent(sessionId, event) {
  if (!sessionId) return;
  const queue = pending.get(sessionId) || [];
  queue.push(event);
  while (queue.length > OPENCODE_PENDING_EVENT_CAP) {
    queue.shift(); // drop the OLDEST entry when the cap is exceeded
  }
  pending.set(sessionId, queue);
}

// --- Turn normalization + transcript capture (plan F1/F2) --------------------
//
// Accepts BOTH raw shapes: the fixture shape ({info:{...time, worktree},
// parts:[{type:text,text}|{type:tool,name,text}]}) and the LIVE SDK shape
// (time as {created:<epoch-ms>}; tool parts {type:tool, tool,
// state:{status,output,error}}). Byte-compatible with the python adapter in
// storelib/mine.py (tests/test_opencode_plugin.js x tests/test_mine_history
// pin the same fixtures on both sides).

function normalizeTs(value) {
  if (value === null || value === undefined || typeof value === "boolean") {
    return null;
  }
  if (typeof value === "number") {
    return new Date(value).toISOString().replace(/\.\d{3}Z$/, "Z");
  }
  if (typeof value === "object" && "created" in value) {
    return normalizeTs(value.created);
  }
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isNaN(parsed)) return null;
    return new Date(parsed).toISOString().replace(/\.\d{3}Z$/, "Z");
  }
  return null;
}

function normalizeBlocks(parts) {
  const blocks = [];
  for (const part of parts || []) {
    if (!part || typeof part !== "object") continue;
    if (part.type === "text") {
      blocks.push({ type: "text", text: part.text || "" });
    } else if (part.type === "tool") {
      const name = part.name || part.tool || "";
      const state = part.state;
      const text = (state && typeof state === "object"
        && (state.output || state.error)) || part.text || "";
      blocks.push({ type: "tool", name, text });
    }
  }
  return blocks;
}

export function normalizeOpenCodeMessage(message, sourcePath, seq) {
  const info = (message && message.info) || {};
  return {
    session_id: info.sessionID || info.session_id || "",
    workdir: info.worktree || info.workdir || "",
    turn_uuid: info.id || "",
    parent_uuid: info.parentID || info.parent_id || null,
    seq: seq,
    ts: normalizeTs(info.time),
    role: info.role || "",
    blocks: normalizeBlocks(message && message.parts),
    source_path: sourcePath,
    harness: "opencode",
  };
}

function transcriptPath() {
  return process.env.ZMEM_OPENCODE_TRANSCRIPTS
    || path.join(process.env.ZMEM_DATA || path.join(homeDir(), ".zmem"),
                 "transcripts", "opencode");
}

function readMaxSeq(target) {
  // Persisted sequence state across reloads: the seq counter is recovered
  // from the transcript's own tail (the transcript IS the seq state).
  try {
    const raw = fs.readFileSync(target, "utf-8");
    const lines = raw.split("\n").filter((l) => l.trim());
    for (let i = lines.length - 1; i >= 0; i--) {
      try {
        const turn = JSON.parse(lines[i]);
        if (typeof turn.seq === "number") return turn.seq;
      } catch { /* skip malformed tail lines */ }
    }
  } catch { /* absent transcript starts at -1 */ }
  return -1;
}

const finalizedTurns = new Set(); // turn_uuid idempotency (in-process)
let seqCounter = null;

export function appendTurn(message, sourcePath) {
  const target = transcriptPath();
  try {
    const info = (message && message.info) || {};
    const turnUuid = info.id || "";
    if (!turnUuid || finalizedTurns.has(turnUuid)) return false; // exactly once
    if (seqCounter === null) seqCounter = readMaxSeq(target);
    const seq = seqCounter + 1;
    const turn = normalizeOpenCodeMessage(message, sourcePath, seq);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.appendFileSync(target,
      JSON.stringify(turn) + "\n", "utf-8");
    seqCounter = seq;
    finalizedTurns.add(turnUuid);
    return true;
  } catch (exc) {
    // Fail-open capture: a non-directory or unreadable override logs ONE
    // diagnostic and performs no write (the mining CLI owns the exit-1
    // refusal shape for unreadable transcript dirs).
    try {
      process.stderr.write(
        "[zmem-opencode] transcript append skipped: " + String(exc) + "\n");
    } catch { /* best-effort */ }
    return false;
  }
}

export function _resetCaptureStateForTests() {
  finalizedTurns.clear();
  seqCounter = null;
  openMessages.clear();
}

// Message capture engine: aggregates SDK message events by (sessionID,
// messageID); a terminal-state message finalizes immediately; a
// non-terminal one finalizes on the NEXT system transform — exactly once
// either way.
const openMessages = new Map(); // messageID -> {sessionID, message, terminal}

export function _ingestMessageEvent(event) {
  if (!event || typeof event !== "object") return;
  // Message events carry the message either nested (event.message) or
  // flattened onto the event itself (info/parts at the top level); part
  // events carry a single `part` that merges into the aggregate.
  const payload = event.message || event;
  const info = payload.info || payload;
  const messageID = info.id;
  if (!messageID) return;
  const sessionID = info.sessionID || info.session_id || "";
  const status = info.status || payload.status;
  const terminal = status === "completed" || status === "aborted"
    || status === "error" || status === "cancelled";
  const existing = openMessages.get(messageID)
    || { sessionID, message: { info: { ...info }, parts: [] } };
  // Merge: the latest event's info wins (status transitions), parts union
  // in first-seen order, a single `part` payload appends when new.
  existing.message.info = { ...existing.message.info, ...info };
  const parts = payload.parts || info.parts || [];
  const known = new Set(existing.message.parts.map((p) => JSON.stringify(p)));
  for (const part of parts) {
    const key = JSON.stringify(part);
    if (!known.has(key)) { existing.message.parts.push(part); known.add(key); }
  }
  if (event.part && !known.has(JSON.stringify(event.part))) {
    existing.message.parts.push(event.part);
  }
  existing.terminal = terminal || existing.terminal;
  openMessages.set(messageID, existing);
  if (existing.terminal) {
    finalizeMessage(messageID);
  }
}

function finalizeMessage(messageID) {
  const entry = openMessages.get(messageID);
  if (!entry) return;
  openMessages.delete(messageID);
  appendTurn(entry.message, path.basename(transcriptPath()));
}

function finalizePendingMessages() {
  for (const messageID of Array.from(openMessages.keys())) {
    finalizeMessage(messageID);
  }
}

// --- Plugin entry: live SDK hook wiring --------------------------------------

export default async function zmemPlugin(input, options = {}) {
  const opts = options || {};
  const storeDirectory = opts.storeDirectory
    || (input && input.worktree && process.env.ZMEM_DATA) || undefined;
  const pluginEnv = buildOpenCodeEnv({
    storeDirectory,
    worktree: input && input.worktree,
    sessionId: "",
  });
  for (const [key, value] of Object.entries(pluginEnv)) {
    if (value !== "" && !process.env[key]) process.env[key] = value;
  }

  let lastSessionId = "";

  function sessionIdFor(inputArg) {
    return (inputArg && inputArg.sessionID)
      || (inputArg && inputArg.sessionId) || lastSessionId || "";
  }

  return {
    // Map the historical probe selection onto the live hook names: the
    // selected probe's `system.transform` IS the SDK's
    // experimental.chat.system.transform; `session.compacting` IS
    // experimental.session.compacting; `event` is unchanged.
    "experimental.chat.system.transform": async (hookInput, output) => {
      const sessionId = sessionIdFor(hookInput);
      drainPending(sessionId);          // drained exactly once
      finalizePendingMessages();        // non-terminal messages finalize here
      const envelope = await runInjection({
        query: "",
        moment: "user_prompt",
        session_id: sessionId,
        budget_tokens: 1500,
      });
      if (envelope && envelope.rendered
          && Array.isArray(output.system)) {
        output.system.push(envelope.rendered); // exactly one non-empty fence
      }
    },
    "experimental.session.compacting": async (hookInput, output) => {
      const sessionId = sessionIdFor(hookInput);
      const envelope = await runInjection({
        query: "",
        moment: "precompact",
        session_id: sessionId,
        budget_tokens: 1500,
      });
      if (envelope && envelope.rendered
          && Array.isArray(output.context)) {
        output.context.push(envelope.rendered);
      }
      // Never writes a user prompt: compaction appends to context only.
    },
    event: async (hookInput) => {
      const evt = hookInput && hookInput.event;
      if (!evt || typeof evt !== "object") return;
      const sessionID = evt.sessionID || evt.session_id || "";
      if (sessionID) lastSessionId = sessionID;
      const type = evt.type || "";
      if (type.startsWith("message")) {
        _ingestMessageEvent(evt); // capture engine (aggregation + finalize)
      }
      bufferEvent(sessionID, evt); // pending-injection buffer (capped)
    },
    tool: {
      zmem_recall: tool({
        description: "Recall memories from the shared ZMem store "
          + "(OpenCode adapter, issue #66).",
        args: {
          query: { type: "string", description: "recall query" },
          namespace: { type: "string", description: "optional namespace" },
          session_id: { type: "string", description: "optional session id" },
          limit: { type: "number", description: "rows (default 5)" },
          global_limit: { type: "number", description: "user:global rows (default 3)" },
          budget_tokens: { type: "number", description: "token budget (default 1500)" },
        },
        execute: async (args, ctx) => {
          const envelope = await runInjection({
            query: args.query,
            moment: "user_prompt",
            session_id: args.session_id || (ctx && ctx.sessionID) || "",
            namespace: args.namespace,
            limit: args.limit,
            global_limit: args.global_limit,
            budget_tokens: args.budget_tokens || 1500,
          });
          // ToolResult string arm (plan F2): the JSON string directly.
          return JSON.stringify(envelope);
        },
      }),
      zmem_session_start: tool({
        description: "Session-start memory injection for OpenCode "
          + "(issue #66).",
        args: {
          namespace: { type: "string", description: "optional namespace" },
          session_id: { type: "string", description: "optional session id" },
          limit: { type: "number", description: "rows (default 5)" },
          global_limit: { type: "number", description: "user:global rows (default 3)" },
          budget_tokens: { type: "number", description: "token budget (default 1500)" },
        },
        execute: async (args, ctx) => {
          const envelope = await runInjection({
            query: "",
            moment: "session_start",
            session_id: args.session_id || (ctx && ctx.sessionID) || "",
            namespace: args.namespace,
            limit: args.limit,
            global_limit: args.global_limit,
            budget_tokens: args.budget_tokens || 1500,
          });
          return JSON.stringify(envelope);
        },
      }),
    },
    dispose: async () => {
      finalizePendingMessages();
    },
  };
}
