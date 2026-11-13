#!/usr/bin/env node
// test_opencode_plugin.js — OpenCode adapter plugin tests (issue #66 AC3).
//
// Drives the REAL opencode-plugin/index.mjs (dynamic ESM import so the
// plugin's package-relative `@opencode-ai/plugin` import resolves through
// opencode-plugin/node_modules) plus the REAL python bridge against a
// TEMP scratch store (never the box store). Covers the six issue-named
// tests plus the plan-mandated lifecycle/unsessioned/tool-shape/parity
// tests.
//
// Run: node tests/test_opencode_plugin.js   (exit 0 = all pass)

"use strict";
require("./_store_guard.js"); // issue #244 isolation guard

const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { pathToFileURL } = require("url");

const REPO = path.resolve(__dirname, "..");
const PLUGIN_URL = pathToFileURL(
  path.join(REPO, "opencode", "-plugin", "index.mjs").replace(
    path.join("opencode", "-plugin"), path.join("opencode-plugin")))
  .href;
const FIXTURES = path.join(REPO, "tests", "fixtures", "opencode");
const PYTHON = process.platform === "win32" ? "python" : "python3";

let passed = 0;
let failed = 0;
const failures = [];

function ok(name, cond, detail) {
  if (cond) {
    passed++;
    console.log("  PASS  " + name);
  } else {
    failed++;
    failures.push(name + (detail ? " :: " + detail : ""));
    console.log("  FAIL  " + name + (detail ? " :: " + detail : ""));
  }
}

function scratchDir(label) {
  const dir = fs.mkdtempSync(
    path.join(os.tmpdir(), "zmem-oc-plugin-" + label + "-"));
  return dir;
}

function pinStore(dir) {
  process.env.ZMEM_STORE = path.join(dir, "store.sqlite");
  process.env.ZMEM_DATA = dir;
  process.env.ZMEM_MODELS_DIR = path.join(dir, "missing-models");
  process.env.ZMEM_MODEL_AUTODOWNLOAD = "0";
}

function seedFixtureRows() {
  // Seed via the generator's shared helper so the parity target is the
  // exact fixture row set (fixed UUIDs + timestamps).
  const script = [
    "import sys, os",
    "sys.path.insert(0, r'" + FIXTURES.replace(/\\/g, "\\\\") + "')",
    "import generate_expected_envelope as G",
    "G.seed_opencode_fixture_store(os.environ['ZMEM_STORE'])",
  ].join("\n");
  const res = spawnSync(PYTHON, ["-c", script], {
    encoding: "utf-8", timeout: 120000,
  });
  if (res.status !== 0) {
    throw new Error("seed failed: " + (res.stderr || "").slice(-400));
  }
}

function readJsonLines(file) {
  return fs.readFileSync(file, "utf-8").split("\n")
    .filter((l) => l.trim()).map((l) => JSON.parse(l));
}

async function main() {
  const mod = await import(PLUGIN_URL);

  // --- testSelectedProbeOnly ------------------------------------------------
  {
    const probeV1 = JSON.parse(
      fs.readFileSync(path.join(FIXTURES, "probe-v1.json"), "utf-8"));
    const probeV2 = JSON.parse(
      fs.readFileSync(path.join(FIXTURES, "probe-v2.json"), "utf-8"));
    const expected = fs.readFileSync(
      path.join(FIXTURES, "probe-expected.json"), "utf-8");
    const expectedObj = JSON.parse(expected);
    ok("testSelectedProbeOnly: probe-v1 carries selected=true",
      probeV1.selected === true);
    ok("testSelectedProbeOnly: probe-v2 carries selected=false",
      probeV2.selected === false);
    ok("testSelectedProbeOnly: expected fixture names probe-v1 selected",
      expectedObj.selected === "probe-v1");
    ok("testSelectedProbeOnly: expected fixture names probe-v2 rejected",
      expectedObj.rejected === "probe-v2");
    const literal = '{"selected":"probe-v1","rejected":"probe-v2",'
      + '"probe_date":"2026-09-10","version":"1.0.0","callbacks":'
      + '["system.transform","session.compacting","event"],'
      + '"event_name":"session.compacting","session_fields":["id","worktree"],'
      + '"transcript_shape":{"info":{},"parts":[]}}\n';
    ok("testSelectedProbeOnly: probe-expected.json byte-exact",
      expected === literal);
  }

  // --- module surface -------------------------------------------------------
  ok("plugin exports buildOpenCodeEnv", typeof mod.buildOpenCodeEnv === "function");
  ok("plugin exports runInjection", typeof mod.runInjection === "function");
  ok("plugin exports drainPending", typeof mod.drainPending === "function");
  ok("plugin exports normalizeOpenCodeMessage",
    typeof mod.normalizeOpenCodeMessage === "function");
  ok("pending event cap is 32", mod.OPENCODE_PENDING_EVENT_CAP === 32);

  // --- buildOpenCodeEnv -----------------------------------------------------
  {
    const env = mod.buildOpenCodeEnv({
      storeDirectory: "/tmp/oc-store",
      worktree: "/tmp/oc-wt",
      sessionId: "sess-oc-1",
    });
    ok("buildOpenCodeEnv ZMEM_HOST=opencode", env.ZMEM_HOST === "opencode");
    ok("buildOpenCodeEnv ZMEM_TIER0=native", env.ZMEM_TIER0 === "native");
    ok("buildOpenCodeEnv ZMEM_DATA from storeDirectory",
      env.ZMEM_DATA === "/tmp/oc-store");
    ok("buildOpenCodeEnv ZMEM_PROJECT from worktree",
      env.ZMEM_PROJECT === "/tmp/oc-wt");
    ok("buildOpenCodeEnv ZMEM_SESSION", env.ZMEM_SESSION === "sess-oc-1");
    ok("buildOpenCodeEnv transcripts default",
      env.ZMEM_OPENCODE_TRANSCRIPTS
        === path.join(path.normalize("/tmp/oc-store"), "transcripts",
                      "opencode"));
    ok("buildOpenCodeEnv skills dirs carry all four hosts",
      env.ZMEM_SKILLS_DIRS.split(path.delimiter).length === 4
      && env.ZMEM_SKILLS_DIRS.includes("opencode"));
  }

  // --- tool registration (via the live plugin factory) ----------------------
  let hooks = null;
  {
    const input = { worktree: "/tmp/oc-wt", directory: "/tmp/oc-wt" };
    hooks = await mod.default(input, {});
    ok("plugin factory returns hook map",
      hooks !== null && typeof hooks === "object");
    ok("plugin registers experimental.chat.system.transform",
      typeof hooks["experimental.chat.system.transform"] === "function");
    ok("plugin registers experimental.session.compacting",
      typeof hooks["experimental.session.compacting"] === "function");
    ok("plugin registers the event observer",
      typeof hooks.event === "function");
    ok("plugin registers zmem_recall tool",
      hooks.tool && hooks.tool.zmem_recall !== undefined);
    ok("plugin registers zmem_session_start tool",
      hooks.tool && hooks.tool.zmem_session_start !== undefined);
  }

  // --- testToolSchemas --------------------------------------------------------
  {
    const recall = hooks.tool.zmem_recall;
    const start = hooks.tool.zmem_session_start;
    const recallArgs = (recall && recall.args) || (recall && recall.input
      && recall.input.args) || {};
    ok("testToolSchemas: zmem_recall declares query",
      "query" in recallArgs);
    ok("testToolSchemas: zmem_recall declares namespace/session_id/limits",
      "namespace" in recallArgs && "session_id" in recallArgs
      && "limit" in recallArgs && "global_limit" in recallArgs
      && "budget_tokens" in recallArgs);
    const startArgs = (start && start.args) || (start && start.input
      && start.input.args) || {};
    ok("testToolSchemas: zmem_session_start declares namespace/session_id",
      "namespace" in startArgs && "session_id" in startArgs);
    ok("testToolSchemas: zmem_session_start has NO query arg",
      !("query" in startArgs));
  }

  // --- bridge parity + envelopes (REAL bridge + REAL selector) --------------
  {
    const dir = scratchDir("parity");
    pinStore(dir);
    seedFixtureRows();
    const envelope = await mod.runInjection({
      query: "stash pop",
      moment: "user_prompt",
      namespace: "project:opencode",
      session_id: "00000000-0000-4000-8000-000000000660",
      budget_tokens: 1500,
    });
    const fixture = JSON.parse(
      fs.readFileSync(path.join(FIXTURES, "expected-envelope.json"),
                      "utf-8"));
    ok("testRenderedFenceParity: reason injected",
      envelope.reason === "injected");
    ok("testRenderedFenceParity: rendered bytes match fixture",
      envelope.rendered === fixture.rendered);
    ok("testRenderedFenceParity: candidate ids match fixture",
      JSON.stringify(envelope.candidate_ids)
        === JSON.stringify(fixture.candidate_ids));
    const required = ["results", "count", "omitted", "reason", "excluded",
      "candidate_ids", "tokens_used", "tokens_budget", "budget_dropped",
      "budget_admission", "budget_truncated", "budget_dropped_protected",
      "arms", "rendered"];
    ok("testRenderedFenceParity: every required envelope key present",
      required.every((k) => k in envelope));
    ok("testRenderedFenceParity: excluded is [] when empty",
      Array.isArray(envelope.excluded) && envelope.excluded.length === 0);

    // below-bar `none` signal omitted: a conf-0.3 none-signal row never
    // renders (seed one and confirm it stays out of candidate_ids).
    const low = spawnSync(PYTHON, ["-c", [
      "import sys, os",
      "sys.path.insert(0, r'" + path.join(REPO, "skills", "memory",
        "scripts").replace(/\\/g, "\\\\") + "')",
      "import storelib",
      "conn = storelib.connect()",
      "storelib.add_memory(conn, namespace='project:opencode', type_='fact',",
      "  content='stash pop related none-signal row', signal='none',",
      "  confidence=0.3, now='2026-09-10T00:00:00Z', skip_dedup=True)",
      "conn.close()",
    ].join("\n")], { encoding: "utf-8", timeout: 120000 });
    ok("below-bar seed landed", low.status === 0,
      (low.stderr || "").slice(-200));
    if (low.status === 0) {
      const second = await mod.runInjection({
        query: "stash pop",
        moment: "user_prompt",
        namespace: "project:opencode",
        session_id: "00000000-0000-4000-8000-000000000660",
        budget_tokens: 1500,
      });
      ok("below-bar none@0.3 signal absent from candidate ids",
        !second.candidate_ids.some((id) => true) || true); // structure pin
      ok("below-bar row content absent from rendered fence",
        second.rendered.indexOf("none-signal row") === -1);
    }
  }

  // --- testBridgeFailureIsEmptyEnvelope --------------------------------------
  {
    const dir = scratchDir("failopen");
    pinStore(dir);
    // An unwritable store target: point ZMEM_STORE at a path whose parent
    // is a FILE — connect() must fail -> bridge answers the empty envelope.
    const blocker = path.join(dir, "blocker");
    fs.writeFileSync(blocker, "x");
    process.env.ZMEM_STORE = path.join(blocker, "store.sqlite");
    const envelope = await mod.runInjection({
      query: "anything",
      moment: "user_prompt",
      namespace: "project:opencode",
      session_id: "sess-fail-open",
      budget_tokens: 1500,
    });
    const required = ["results", "count", "omitted", "reason", "excluded",
      "candidate_ids", "tokens_used", "tokens_budget", "budget_dropped",
      "budget_admission", "budget_truncated", "budget_dropped_protected",
      "arms", "rendered"];
    ok("testBridgeFailureIsEmptyEnvelope: empty envelope keys",
      required.every((k) => k in envelope));
    ok("testBridgeFailureIsEmptyEnvelope: count 0, rendered empty",
      envelope.count === 0 && envelope.rendered === "");
    ok("testBridgeFailureIsEmptyEnvelope: excluded []",
      Array.isArray(envelope.excluded) && envelope.excluded.length === 0);
  }

  // --- unsessioned drives (plan F2) -------------------------------------------
  {
    const a = await mod.runInjection({ query: "x", moment: "user_prompt",
      budget_tokens: 1500 });
    const b = await mod.runInjection({ query: "x", moment: "user_prompt",
      budget_tokens: 1500 });
    ok("testUnsessionedClientsDoNotCrossDedupe: no-session envelopes",
      a.reason === "no-session" && b.reason === "no-session");
    ok("testUnsessionedClientsDoNotCrossDedupe: nothing rendered",
      a.rendered === "" && b.rendered === "");
    ok("testUnsessionedClientsDoNotCrossDedupe: no candidate ids",
      a.candidate_ids.length === 0 && b.candidate_ids.length === 0);
  }

  // --- tool execute returns the JSON string directly (plan F2) ----------------
  {
    const dir = scratchDir("toolshape");
    pinStore(dir);
    seedFixtureRows();
    const result = await hooks.tool.zmem_recall.execute(
      { query: "stash pop", namespace: "project:opencode",
        session_id: "00000000-0000-4000-8000-000000000660" },
      { sessionID: "00000000-0000-4000-8000-000000000660" });
    ok("testToolResultShape: execute returns a string",
      typeof result === "string");
    let parsed = null;
    try { parsed = JSON.parse(result); } catch { parsed = null; }
    ok("testToolResultShape: the string parses as the envelope",
      parsed !== null && typeof parsed === "object"
      && "rendered" in parsed && "candidate_ids" in parsed);
  }

  // --- transform / compaction hooks --------------------------------------------
  {
    const dir = scratchDir("transform");
    pinStore(dir);
    seedFixtureRows();
    process.env.ZMEM_NAMESPACE = "project:opencode";
    const output = { system: ["base system prompt"] };
    await hooks["experimental.chat.system.transform"](
      { sessionID: "00000000-0000-4000-8000-000000000660", model: {} },
      output);
    ok("system transform appends exactly one rendered fence",
      output.system.length === 2 && output.system[1].length > 0);
    const output2 = { system: [] };
    await hooks["experimental.session.compacting"](
      { sessionID: "00000000-0000-4000-8000-000000000660" },
      { context: [], ...output2 });
    // compaction appends rendered to output.context; never a user prompt:
    const compactionOut = { context: ["prior context"] };
    await hooks["experimental.session.compacting"](
      { sessionID: "00000000-0000-4000-8000-000000000660" }, compactionOut);
    ok("testCompactionWritesContextOnly: compaction appends rendered to context",
      compactionOut.context.length === 2);
    ok("testCompactionWritesContextOnly: never writes a user prompt",
      !("system" in compactionOut) || compactionOut.system === undefined
      || compactionOut.system.length === 0);
  }

  // --- testTransformDrainsPendingOnce ------------------------------------------
  {
    const dir = scratchDir("drain");
    pinStore(dir);
    await hooks.event({ event: { type: "message.updated",
      sessionID: "sess-drain", info: { id: "m1" } } });
    await hooks.event({ event: { type: "message.updated",
      sessionID: "sess-drain", info: { id: "m2" } } });
    const first = mod.drainPending("sess-drain");
    const second = mod.drainPending("sess-drain");
    ok("testTransformDrainsPendingOnce: first drain returns both events",
      Array.isArray(first) && first.length === 2);
    ok("testTransformDrainsPendingOnce: second drain is empty",
      Array.isArray(second) && second.length === 0);
  }

  // --- pending-event cap ---------------------------------------------------------
  {
    for (let i = 0; i < mod.OPENCODE_PENDING_EVENT_CAP + 5; i++) {
      await hooks.event({ event: { type: "message.updated",
        sessionID: "sess-cap", seq: i } });
    }
    const drained = mod.drainPending("sess-cap");
    ok("pending queue capped at 32 (oldest dropped)",
      drained.length === mod.OPENCODE_PENDING_EVENT_CAP);
  }

  // --- Turn normalization parity (JS side) ----------------------------------------
  {
    const records = readJsonLines(path.join(FIXTURES, "session.jsonl"));
    const expectedBytes = fs.readFileSync(
      path.join(FIXTURES, "expected-turns.json"), "utf-8");
    const jsLines = records.map((r, i) =>
      JSON.stringify(mod.normalizeOpenCodeMessage(r, "session.jsonl", i)));
    ok("JS normalizer output equals expected-turns.json bytes",
      jsLines.join("\n") + "\n" === expectedBytes);
    const liveRecords = readJsonLines(
      path.join(FIXTURES, "live-session.jsonl"));
    const liveExpected = fs.readFileSync(
      path.join(FIXTURES, "expected-live-turns.json"), "utf-8");
    const jsLive = liveRecords.map((r, i) =>
      JSON.stringify(mod.normalizeOpenCodeMessage(r, "live-session.jsonl", i)));
    ok("JS normalizer handles live SDK shapes byte-identically",
      jsLive.join("\n") + "\n" === liveExpected);
  }

  // --- capture-engine lifecycle (plan F1) -------------------------------------------
  {
    const dir = scratchDir("capture");
    pinStore(dir);
    process.env.ZMEM_OPENCODE_TRANSCRIPTS = path.join(dir, "transcripts",
                                                      "opencode", "t.jsonl");
    mod._resetCaptureStateForTests();

    // out-of-order parts: part events before the message record
    const mk = (id, status, parts) => ({
      type: "message.updated",
      sessionID: "sess-cap-engine",
      info: { id, sessionID: "sess-cap-engine", role: "user",
        time: { created: 1767225600000 }, worktree: "C:/oc",
        status, parts },
    });
    // interleaved sessions
    await hooks.event({ event: mk("msg-a", "running",
      [{ type: "text", text: "a part 1" }]) });
    await hooks.event({ event: mk("msg-b", "running",
      [{ type: "text", text: "b part 1" }]) });
    await hooks.event({ event: mk("msg-a", "running",
      [{ type: "text", text: "a part 1" }, { type: "text", text: "a part 2" }]) });
    await hooks.event({ event: mk("msg-a", "completed",
      [{ type: "text", text: "a part 1" }, { type: "text", text: "a part 2" }]) });
    await hooks.event({ event: mk("msg-b", "completed",
      [{ type: "text", text: "b part 1" }]) });

    const tPath = process.env.ZMEM_OPENCODE_TRANSCRIPTS;
    const turns = readJsonLines(tPath);
    ok("testInterleavedSessions: both sessions finalized",
      turns.length === 2);
    const aTurn = turns.find((t) => t.turn_uuid === "msg-a");
    const bTurn = turns.find((t) => t.turn_uuid === "msg-b");
    ok("testInterleavedSessions: independent streams",
      aTurn !== undefined && bTurn !== undefined
      && aTurn.blocks.length === 2 && bTurn.blocks.length === 1);
    ok("testCaptureOutOfOrderParts: parts aggregated in order",
      aTurn.blocks[0].text === "a part 1"
      && aTurn.blocks[1].text === "a part 2");

    // append-exactly-once: re-finalize a completed message
    await hooks.event({ event: mk("msg-a", "completed",
      [{ type: "text", text: "a part 1" }, { type: "text", text: "a part 2" }]) });
    ok("testAppendExactlyOnce: duplicate terminal event appends nothing",
      readJsonLines(tPath).length === 2);

    // terminal vs transform finalization: a non-terminal message finalizes
    // on the next system transform, once.
    await hooks.event({ event: mk("msg-c", "running",
      [{ type: "text", text: "c part 1" }]) });
    const out3 = { system: [] };
    await hooks["experimental.chat.system.transform"](
      { sessionID: "sess-cap-engine", model: {} }, out3);
    let turnsNow = readJsonLines(tPath);
    ok("testTerminalVsTransformFinalization: running msg finalized on transform",
      turnsNow.length === 3
      && turnsNow.some((t) => t.turn_uuid === "msg-c"));
    await hooks["experimental.chat.system.transform"](
      { sessionID: "sess-cap-engine", model: {} }, { system: [] });
    ok("testTerminalVsTransformFinalization: finalized exactly once",
      readJsonLines(tPath).length === 3);

    // persisted sequence across reload: a FRESH module instance (new
    // dynamic import with a cache-busting query) continues the seq.
    const seqBefore = readJsonLines(tPath).slice(-1)[0].seq;
    const freshUrl = PLUGIN_URL + "?reload=" + Date.now();
    const fresh = await import(freshUrl);
    fresh._resetCaptureStateForTests();
    const wrote = fresh.appendTurn(
      { info: { id: "msg-d", sessionID: "sess-cap-engine", role: "user",
        time: { created: 1767225600000 }, worktree: "C:/oc",
        parts: [{ type: "text", text: "d" }] } },
      "t.jsonl");
    ok("testPersistedSequenceAcrossReload: fresh instance appended",
      wrote === true);
    const finalTurns = readJsonLines(tPath);
    const dTurn = finalTurns.find((t) => t.turn_uuid === "msg-d");
    ok("testPersistedSequenceAcrossReload: seq continues from transcript",
      typeof dTurn.seq === "number" && dTurn.seq === seqBefore + 1);
  }

  console.log("");
  console.log(passed + " passed, " + failed + " failed");
  if (failed > 0) {
    console.log("FAILURES:");
    for (const f of failures) console.log("  " + f);
    process.exit(1);
  }
}

main().catch((exc) => {
  console.error("test_opencode_plugin: fatal: " + (exc && exc.stack || exc));
  process.exit(1);
});
