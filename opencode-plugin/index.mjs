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
import { z } from "zod";

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

// (The stream-aware UTF-8 decoder for pipe chunks lives INSIDE callBridge
// since the round-2 review: per-chunk toString("utf-8") corrupts a multibyte
// codepoint split across chunk boundaries, and a module-level singleton
// would share stream state across concurrent bridge calls.)

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
//
// Bounds (review R2/R3/R30/F-004/F-005): the spawn carries a hard 30s
// timeout with kill + empty-envelope fallback (deepseek precedent), the
// stdin stream carries an error listener (a child that exits before the
// write flushes would otherwise crash the host process), and stderr is
// drained and surfaced on abnormal exits.

const BRIDGE_TIMEOUT_MS = 30000;

async function callBridge(request, spawnEnv) {
  return await new Promise((resolve) => {
    const child = spawn(PYTHON, [BRIDGE], {
      stdio: ["pipe", "pipe", "pipe"],
      env: spawnEnv,
    });
    // Per-call decoder (review round-2 F3): concurrent callBridge calls
    // must not share TextDecoder stream state; a module-level singleton
    // would corrupt interleaved multi-byte output. TextDecoder(stream)
    // instead of per-chunk toString: a multibyte codepoint split across
    // pipe chunks would decode as U+FFFD pairs.
    const decoder = new TextDecoder("utf-8");
    let out = "";
    let errTail = "";
    let settled = false;
    const finish = (value) => {
      if (!settled) { settled = true; clearTimeout(timer); resolve(value); }
    };
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* already gone */ }
      finish(emptyEnvelope(request.budget_tokens || 1500));
    }, BRIDGE_TIMEOUT_MS);
    child.stdout.on("data", (chunk) => {
      out += decoder.decode(chunk, { stream: true });
    });
    child.stderr.on("data", (chunk) => {
      errTail = (errTail + chunk.toString("utf-8")).slice(-800);
    });
    // A child that exits before the stdin write flushes emits an error on
    // the stdin stream; without this listener it is an uncaughtException
    // (host crash). Drain and let the close/error arms settle the promise.
    child.stdin.on("error", () => {});
    child.on("error", (exc) => {
      try {
        process.stderr.write("[zmem-opencode] bridge spawn failed: "
          + String(exc && exc.message || exc) + "\n");
      } catch { /* best-effort */ }
      finish(emptyEnvelope(request.budget_tokens || 1500));
    });
    child.on("close", (code) => {
      // decode() with NO options flushes the trailing partial codepoint
      // (a {stream:true} empty decode would wedge stale bytes in the
      // decoder for the next call — review round-2 F3).
      out += decoder.decode();
      try {
        const parsed = JSON.parse(out);
        if (isEnvelope(parsed)) {
          if (!Array.isArray(parsed.excluded)) parsed.excluded = [];
          finish(parsed);
        } else {
          finish(emptyEnvelope(request.budget_tokens || 1500));
        }
      } catch {
        if (errTail.trim()) {
          try {
            process.stderr.write("[zmem-opencode] bridge stderr (tail): "
              + errTail.trim() + "\n");
          } catch { /* best-effort */ }
        }
        finish(emptyEnvelope(request.budget_tokens || 1500));
      }
    });
    child.stdin.write(JSON.stringify(request), "utf-8");
    child.stdin.end();
  });
}

export async function runInjection(request, spawnEnv) {
  const req = request || {};
  if (!req.session_id) {
    // Unknown session: fail open WITHOUT a ledger entry (plan F2) — no
    // bridge call, no shared injection identity.
    return emptyEnvelope(req.budget_tokens || 1500, "no-session");
  }
  return await callBridge(req, spawnEnv || process.env);
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

// ZMEM_OPENCODE_TRANSCRIPTS (and the <ZMEM_DATA>/transcripts/opencode
// default) is a DIRECTORY of *.jsonl transcripts per the mining contract
// (store.py mine-history --source opencode globs <dir>/*.jsonl); a path
// ending in .jsonl is honored as a direct FILE override for test callers.
function transcriptRoot() {
  return process.env.ZMEM_OPENCODE_TRANSCRIPTS
    || path.join(process.env.ZMEM_DATA || path.join(homeDir(), ".zmem"),
                 "transcripts", "opencode");
}

function transcriptFileFor(sessionId) {
  const root = transcriptRoot();
  if (root.endsWith(".jsonl")) return root; // explicit file override
  const safe = String(sessionId || "session").replace(/[^A-Za-z0-9._-]/g, "_");
  return path.join(root, safe + ".jsonl");
}

function readMaxSeq(target) {
  // Persisted capture state across reloads: the seq counter AND the seen
  // turn_uuid set are recovered from the transcript file itself (review
  // R10 — the old code recovered seq but not uuids, so a replayed event
  // after reload appended a duplicate turn).
  try {
    const raw = fs.readFileSync(target, "utf-8");
    const lines = raw.split("\n").filter((l) => l.trim());
    let maxSeq = -1;
    for (const line of lines) {
      try {
        const turn = JSON.parse(line);
        if (typeof turn.seq === "number" && turn.seq > maxSeq) {
          maxSeq = turn.seq;
        }
        if (turn.turn_uuid) finalizedTurns.add(turn.turn_uuid);
      } catch { /* skip malformed lines */ }
    }
    return maxSeq;
  } catch { /* absent transcript starts at -1 */ }
  return -1;
}

const finalizedTurns = new Set(); // turn_uuid idempotency (durable via transcript)
const seqCounters = new Map();    // per-FILE seq state (per session file)

// Capture-path secret redaction (review R6/FB-009, round-3 G3; the
// deepseek adapter's always-auto precedent): the canonical transcript is
// a PERSISTED ARCHIVE, so secret-like content is redacted at the write
// boundary — never stored raw. This is the high-precision credential core
// of skills/memory/scripts/redaction.py SECRET_CREDENTIAL_PATTERNS with
// the same [REDACTED_SECRET] marker; the python miner still runs the full
// redact_text pipeline downstream (defense in depth). The generic entropy
// patterns (base64-40+/hex-32+) are intentionally NOT ported — too
// false-positive-prone for a capture path; the python lane still owns them.
const REDACTED_SECRET = "[REDACTED_SECRET]";
const CAPTURE_KEYVALUE_SECRET = /(api[_-]?key|secret|token|password|passwd|pwd|private[_-]?key)(\s*[:=]\s*)(\S{8,})/gi;
const CAPTURE_SECRET_SIMPLE = [
  /\bBearer\s+[A-Za-z0-9._+/~-]{16,}(?![A-Za-z0-9._+/~-])/g,
  /\bgh[pousr]_[A-Za-z0-9]{36,}\b/g,
  /\bgithub_pat_[A-Za-z0-9_]{20,}\b/g,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])/g,
  /\bsk-(?:ant|proj)-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])/g,
  /\bsk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])/g,
  /\beyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}(?![0-9A-Za-z_-])/g,
  /-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |)PRIVATE KEY-----/g,
  /-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----/g,
];

function redactCaptureText(text) {
  let out = String(text == null ? "" : text);
  let count = 0;
  // key=value span semantics (issue #180 parity): keep key + separator,
  // mask only the value; a value already equal to the marker is detected
  // (counted) but not rewritten.
  out = out.replace(CAPTURE_KEYVALUE_SECRET,
    (match, key, sep, value) => {
      count += 1;
      if (value === REDACTED_SECRET) return match;
      return key + sep + REDACTED_SECRET;
    });
  for (const re of CAPTURE_SECRET_SIMPLE) {
    out = out.replace(re, (match) => {
      count += 1;
      return match === REDACTED_SECRET ? match : REDACTED_SECRET;
    });
  }
  return { text: out, count };
}

export function _redactCaptureTextForTests(text) {
  return redactCaptureText(text);
}

export function appendTurn(message, sourcePath) {
  try {
    // Capture kill switch (review R6/UR-3 — deepseek ZMEM_CAPTURE parity):
    // production default is ON; "0" disables transcript capture entirely.
    if ((process.env.ZMEM_CAPTURE || "") === "0") return false;
    const info = (message && message.info) || {};
    const turnUuid = info.id || "";
    // Hydrate FIRST (review round-2 F2): on reload the first appendTurn
    // for a file may itself be a REPLAYED turn — the id check must run
    // after readMaxSeq has recovered the seen-uuid set from the transcript.
    const target = transcriptFileFor(info.sessionID || info.session_id);
    if (!seqCounters.has(target)) {
      seqCounters.set(target, readMaxSeq(target));
    }
    if (!turnUuid || finalizedTurns.has(turnUuid)) return false; // exactly once
    const seq = seqCounters.get(target) + 1;
    const turn = normalizeOpenCodeMessage(message, sourcePath, seq);
    // Redact BEFORE persistence (round-3 G3): the archive never stores
    // raw secret-like block text; a redaction flags the turn.
    let redactions = 0;
    turn.blocks = (turn.blocks || []).map((b) => {
      if (b && typeof b === "object" && typeof b.text === "string") {
        const r = redactCaptureText(b.text);
        redactions += r.count;
        return r.count ? { ...b, text: r.text } : b;
      }
      return b;
    });
    if (redactions) turn.secret_warning = true;
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.appendFileSync(target,
      JSON.stringify(turn) + "\n", "utf-8");
    seqCounters.set(target, seq);
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
  seqCounters.clear();
  openMessages.clear();
}

// Message capture engine (review R1/F-001 rewrite): the pinned SDK
// (@opencode-ai/sdk 1.18.3) wraps every event payload in `properties`
// (EventMessageUpdated = {type, properties:{info: Message}};
// EventMessagePartUpdated = {type, properties:{part}}); the fixture/
// legacy vocabulary carries the same fields flat. Both shapes are read.
// Terminality: the SDK Message type has NO `status` field — an assistant
// message is finished when `time.completed` (or time.error) exists, and a
// user message arrives complete. Parts are keyed by part id and replaced
// in place (streamed part updates must not accumulate snapshots).
const openMessages = new Map(); // messageID -> aggregate

function readEventEnvelope(event) {
  if (!event || typeof event !== "object") return null;
  const props = (event.properties && typeof event.properties === "object")
    ? event.properties
    : event; // fixture/legacy flattened shape
  const raw = props.message || props.info || null;
  const info = (raw && typeof raw === "object" && raw.info
    && typeof raw.info === "object") ? raw.info : raw;
  return {
    props,
    info: (info && typeof info === "object") ? info : null,
    part: (props.part && typeof props.part === "object") ? props.part : null,
    sessionID: props.sessionID
      || ((info && typeof info === "object") ? info.sessionID : "") || "",
  };
}

function messageFinished(info) {
  if (!info || typeof info !== "object") return false;
  if (info.role === "user") return true; // user messages arrive complete
  const time = info.time;
  // SDK AssistantMessage carries errors at the TOP level (info.error);
  // time.error covers the flat fixture vocabulary.
  return !!(time && typeof time === "object"
    && (time.completed || time.error)) || info.error != null;
}

export function _ingestMessageEvent(event) {
  if (!event || typeof event !== "object") return;
  const envelope = readEventEnvelope(event);
  if (!envelope) return;
  const { info, part, sessionID } = envelope;

  // Part-delta event (SDK: message.part.updated carries properties.part
  // with .messageID + .id): upsert by part id into the open aggregate.
  // Parts NEVER finalize: a user turn can carry several part events (and
  // delta updates to the same part id), so the aggregate accumulates until
  // a turn boundary (the session's NEXT message.updated, the session's
  // own system transform, or dispose) — round-3 G1.
  if (part && !info) {
    const messageID = part.messageID || "";
    if (!messageID) return;
    let existing = openMessages.get(messageID);
    if (!existing) {
      if (finalizedTurns.has(messageID)) return; // post-finalize straggler
      // Part BEFORE its message snapshot (robust ordering): open a
      // skeleton so early parts are not dropped; the snapshot's info
      // merges in when it arrives.
      existing = { sessionID: part.sessionID || "", info: {},
        partsById: new Map(), terminal: false };
      openMessages.set(messageID, existing);
    }
    if (part.id != null) {
      existing.partsById.set(String(part.id), part);
    } else {
      existing.partsById.set(String(existing.partsById.size), part);
    }
    return;
  }

  if (!info) return;
  const messageID = info.id;
  if (!messageID) return;
  const finished = messageFinished(info);

  let existing = openMessages.get(messageID);
  if (!existing) {
    // Turn boundary (round-3 G1): a NEW message id in a session implies
    // every EARLIER message of that session is complete — flush its open
    // aggregates first (user turns accumulate HERE until this boundary;
    // the session's own transform and dispose also flush).
    const boundarySession = info.sessionID || info.session_id || sessionID;
    if (boundarySession) finalizeUserTurns(boundarySession);
    existing = {
      sessionID: boundarySession,
      info: {},
      partsById: new Map(),
      terminal: false,
    };
  }
  // Latest info wins (session/role/time transitions).
  existing.info = { ...existing.info, ...info };
  existing.sessionID = existing.info.sessionID
    || existing.info.session_id || existing.sessionID || sessionID;
  existing.terminal = finished || existing.terminal;
  // A parts array ON THE INCOMING snapshot replaces the map (authoritative
  // ordering); an update WITHOUT parts leaves the accumulated part deltas
  // intact (a rebuild from the stale merged array would clobber them —
  // review round-2 nit).
  if (Array.isArray(info.parts)) {
    existing.partsById = new Map();
    let i = 0;
    for (const p of info.parts) {
      existing.partsById.set(String((p && p.id != null) ? p.id : i), p);
      i += 1;
    }
  }
  openMessages.set(messageID, existing);
  if (existing.terminal && existing.info.role !== "user") {
    // Assistant terminality is authoritative — SDK part events precede
    // the completed snapshot. User snapshots are NEVER finalized here:
    // their parts stream independently and may continue after this
    // event (round-3 G1; finalize at the next turn boundary / transform /
    // dispose).
    finalizeMessage(messageID);
  }
}

function finalizeMessage(messageID) {
  const entry = openMessages.get(messageID);
  if (!entry) return;
  openMessages.delete(messageID);
  const parts = Array.from(entry.partsById.values());
  const info = { ...entry.info };
  if (!info.sessionID && entry.sessionID) info.sessionID = entry.sessionID;
  // RAW message shape {info, parts} — the same vocabulary normalizeOpen-
  // CodeMessage and the miner's RAW-shape lane read; parts ride TOP LEVEL
  // (info.parts is NOT the capture contract).
  appendTurn({ info, parts },
    path.basename(transcriptFileFor(entry.sessionID)));
}

// Finalize ONLY the transform's own session's open messages (review
// R11/CS-003: the previous all-session finalize truncated still-streaming
// messages of other sessions and latched their uuids, permanently losing
// the completed content). A falsy sessionId finalizes NOTHING — a
// transform without a session identity cannot attribute open aggregates.
function finalizePendingMessages(sessionId) {
  if (!sessionId) return;
  for (const [messageID, entry] of Array.from(openMessages)) {
    if (entry.sessionID !== sessionId) continue;
    finalizeMessage(messageID);
  }
}

function finalizeUserTurns(sessionId) {
  // The turn-boundary flush (round-3 G1): only USER turns (and empty
  // skeletons, which appendTurn drops for lack of a uuid) finalize here —
  // a still-streaming ASSISTANT message owns its lifecycle via its
  // upcoming completed snapshot and must not be cut by a sibling
  // message's arrival.
  if (!sessionId) return;
  for (const [messageID, entry] of Array.from(openMessages)) {
    if (entry.sessionID !== sessionId) continue;
    if (entry.info && entry.info.role === "assistant"
        && !entry.terminal) continue;
    finalizeMessage(messageID);
  }
}

function finalizeAllPending() {
  // dispose-only flush: no further events can arrive.
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
  // Review R39: only the path-carrying keys land in the host process env
  // (read by this module's own helpers). Identity keys (ZMEM_HOST, TIER0,
  // PROJECT, SESSION, SKILLS_DIRS, NAMESPACE) stay OUT of the host env so
  // nested hosts launched from OpenCode are never misattributed; they ride
  // the bridge spawn env instead (see callBridge spawn + hook injection).
  for (const key of ["ZMEM_DATA", "ZMEM_OPENCODE_TRANSCRIPTS",
                     "ZMEM_NAMESPACE"]) {
    const value = pluginEnv[key];
    if (value !== "" && value !== undefined && !process.env[key]) {
      process.env[key] = value;
    }
  }
  // Full child env for the bridge: host env + the complete plugin env,
  // RESOLVED AT CALL TIME (a factory-time snapshot would freeze the host
  // env before the caller's pins — identity keys still reach ONLY this
  // child, never the host's other children or process.env itself).
  function bridgeEnv() {
    // pluginEnv values are DEFAULTS: a host/test pin in process.env wins,
    // an empty value never masks one (buildOpenCodeEnv emits "" for unset
    // optionals and a ~/.zmem default that must not override a pin).
    const env = { ...process.env };
    for (const [key, value] of Object.entries(pluginEnv)) {
      if (value !== "" && value !== undefined && !env[key]) env[key] = value;
    }
    return env;
  }

  function sessionIdFor(inputArg) {
    // Review R13/F-006: no cross-session fallback. A transform without a
    // sessionID fails open with the no-session envelope (the SDK types the
    // field optional; guessing an identity would misattribute the ledger).
    return (inputArg && inputArg.sessionID)
      || (inputArg && inputArg.sessionId) || "";
  }

  return {
    // Map the historical probe selection onto the live hook names: the
    // selected probe's `system.transform` IS the SDK's
    // experimental.chat.system.transform; `session.compacting` IS
    // experimental.session.compacting; `event` is unchanged.
    "experimental.chat.system.transform": async (hookInput, output) => {
      const sessionId = sessionIdFor(hookInput);
      drainPending(sessionId);          // drained exactly once
      finalizePendingMessages(sessionId); // THIS session's open messages only
      const envelope = await runInjection({
        query: "",
        moment: "user_prompt",
        session_id: sessionId,
        worktree: input && input.worktree,
        budget_tokens: 1500,
      }, bridgeEnv());
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
        worktree: input && input.worktree,
        budget_tokens: 1500,
      }, bridgeEnv());
      if (envelope && envelope.rendered
          && Array.isArray(output.context)) {
        output.context.push(envelope.rendered);
      }
      // Never writes a user prompt: compaction appends to context only.
    },
    event: async (hookInput) => {
      const evt = hookInput && hookInput.event;
      if (!evt || typeof evt !== "object") return;
      // Review R52: a non-string truthy evt.type must not throw into the
      // host's event dispatch.
      const type = typeof evt.type === "string" ? evt.type : "";
      const envelope = readEventEnvelope(evt);
      const sessionID = envelope ? envelope.sessionID : "";
      if (type.startsWith("message")) {
        _ingestMessageEvent(evt); // capture engine (aggregation + finalize)
      }
      bufferEvent(sessionID, evt); // pending-injection buffer (capped)
    },
    tool: {
      zmem_recall: tool({
        description: "Recall memories from the shared ZMem store "
          + "(OpenCode adapter, issue #66).",
        // Review S1: the pinned tool() contract is tool<Args extends
        // z.ZodRawShape> — plain descriptor objects fail zod safeParse on
        // a real host. Declare zod schemas (query REQUIRED per the issue
        // contract; the rest optional).
        args: {
          query: z.string().describe("recall query"),
          namespace: z.string().optional()
            .describe("optional namespace override"),
          session_id: z.string().optional()
            .describe("optional session id override"),
          limit: z.number().int().optional().describe("rows (default 5)"),
          global_limit: z.number().int().optional()
            .describe("user:global rows (default 3)"),
          budget_tokens: z.number().int().optional()
            .describe("token budget (default 1500)"),
        },
        execute: async (args, ctx) => {
          const envelope = await runInjection({
            query: args.query,
            moment: "user_prompt",
            session_id: args.session_id || (ctx && ctx.sessionID) || "",
            namespace: args.namespace,
            worktree: (ctx && ctx.worktree) || (input && input.worktree),
            limit: args.limit,
            global_limit: args.global_limit,
            budget_tokens: args.budget_tokens || 1500,
          }, bridgeEnv());
          // ToolResult string arm (plan F2): the JSON string directly.
          return JSON.stringify(envelope);
        },
      }),
      zmem_session_start: tool({
        description: "Session-start memory injection for OpenCode "
          + "(issue #66).",
        args: {
          namespace: z.string().optional()
            .describe("optional namespace override"),
          session_id: z.string().optional()
            .describe("optional session id override"),
          limit: z.number().int().optional().describe("rows (default 5)"),
          global_limit: z.number().int().optional()
            .describe("user:global rows (default 3)"),
          budget_tokens: z.number().int().optional()
            .describe("token budget (default 1500)"),
        },
        execute: async (args, ctx) => {
          const envelope = await runInjection({
            query: "",
            moment: "session_start",
            session_id: args.session_id || (ctx && ctx.sessionID) || "",
            namespace: args.namespace,
            worktree: (ctx && ctx.worktree) || (input && input.worktree),
            limit: args.limit,
            global_limit: args.global_limit,
            budget_tokens: args.budget_tokens || 1500,
          }, bridgeEnv());
          return JSON.stringify(envelope);
        },
      }),
    },
    dispose: async () => {
      // Host shutdown: no further events can arrive, so flush every still-
      // open aggregate (any session — a scoped finalize here would drop
      // half-streamed messages permanently) before clearing the buffer.
      finalizeAllPending();
      pending.clear();
      openMessages.clear();
    },
  };
}
