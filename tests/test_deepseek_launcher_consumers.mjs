// Arm's-length acceptance checks: every launcher.mjs consumer routes
// through the shared deadline+escalation semantics AND preserves its
// existing option forwarding (issue trace 480-481-482, Fix 3, r2).
//
// Frozen seam contract:
//   - runMoment(moment, opts)          opts: { query, session, namespace,
//                                          cwd, env?, spawn?, timeoutMs?,
//                                          escalateGraceMs? }  (MERGED with
//                                          the existing defaults — an
//                                          injected spawn replaces the
//                                          child spawn only, never the
//                                          argv/stdin plumbing; env? MERGES
//                                          over the consumer's env defaults
//                                          — caller keys win, childEnv
//                                          normalization + ZMEM_HOST still
//                                          apply — never replaces them)
//   - runCapture(kind, text, meta, opts)   4th positional opts, same keys
//                                          (env? there is the run()
//                                          either-or: it REPLACES the
//                                          default child env wholesale)
//   - runReflect(session, namespace, opts) 3rd positional opts, same keys
//                                          (env? merges over the
//                                          ZMEM_NAMESPACE/ZMEM_SESSION
//                                          childEnv defaults, same as
//                                          runMoment)
//   - runPolicy(opts)                  single opts { env?, spawn?,
//                                          timeoutMs?, escalateGraceMs? }
//                                          (env? merges over the minimal
//                                          policyEnv resolver defaults)
//   - Timeout forwarding: runMoment / runCapture / runReflect expose
//     `result.raw.timedOut === true`; runPolicy fails open to
//     `{ text: "" }`.
//   - Normal forwarding (well-behaved child): result shapes identical to
//     base — runMoment {envelope, raw}, runCapture {code, result, raw},
//     runReflect {ok, out, raw}, runPolicy {text, reason:"ok"}.
//
// Base: every consumer exists but ignores opts.spawn → the routing
// assertions fail (RED).  ZMEM_STORE/ZMEM_DATA are pinned to a scratch
// dir first so any base-leg real spawn stays hermetic.
//
// Run: node --test tests/test_deepseek_launcher_consumers.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  runCapture,
  runMoment,
  runPolicy,
  runReflect,
} from "../deepseek-plugin/launcher.mjs";

const SCRATCH = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-dscons-"));
process.env.ZMEM_STORE = path.join(SCRATCH, "store.sqlite");
process.env.ZMEM_DATA = SCRATCH;
process.env.ZMEM_MODEL_AUTODOWNLOAD = "0";
process.env.ZMEM_MODELS_DIR = path.join(SCRATCH, "no-models");
delete process.env.ZMEM_REMOTE_URL;
// policyEnv() copies only the resolver keys; this sentinel proves the
// exclusion in the forwarding leg.
process.env.ZMEM_PRINCIPAL = "arm-length-sentinel-principal";

const TIMEOUT_MS = 150;
const GRACE_MS = 100;
const RESOLVE_WINDOW_MS = TIMEOUT_MS + GRACE_MS + 450;
const KILL_WINDOW_MS = 2500;
const ESCALATION_SIGNAL = "SIGKILL";

// Minimal fake stdio: EventEmitter plus the setEncoding no-op run() now
// calls on real child streams right after spawn (FB-016b). The fakes emit
// complete-ASCII Buffers, so the no-op changes no existing leg's bytes.
class FakeStdio extends EventEmitter {
  setEncoding() {}
}

class KillIgnoringChild extends EventEmitter {
  constructor() {
    super();
    this.stdout = new FakeStdio();
    this.stderr = new FakeStdio();
    this.stdin = { write() {}, end() {} };
    this.killCalls = [];
  }
  kill(signal) {
    this.killCalls.push(signal ?? null);
    if (this.killCalls.length >= 2) this.emit("close", null, null);
    return true;
  }
}

class WellBehavedChild extends EventEmitter {
  constructor({ out = "", err = "", code = 0 } = {}) {
    super();
    this.stdout = new FakeStdio();
    this.stderr = new FakeStdio();
    this.writes = [];
    this.stdin = {
      write: (chunk) => this.writes.push(String(chunk)),
      end: () => {},
    };
    this.killCalls = [];
    this._out = out;
    this._err = err;
    this._code = code;
  }
  kill(signal) { this.killCalls.push(signal ?? null); return true; }
  async script() {
    await new Promise((r) => setImmediate(r));
    if (this._out) this.stdout.emit("data", Buffer.from(this._out, "utf8"));
    if (this._err) this.stderr.emit("data", Buffer.from(this._err, "utf8"));
    this.emit("close", this._code, null);
  }
}

function killIgnoringSeam() {
  const fake = new KillIgnoringChild();
  const calls = [];
  const spawn = (cmd, args, opts) => { calls.push([cmd, args, opts]); return fake; };
  return { fake, spawn, calls };
}

function wellBehavedSeam(opts) {
  const fake = new WellBehavedChild(opts);
  const calls = [];
  const spawn = (cmd, args, sopts) => { calls.push([cmd, args, sopts]); fake.script(); return fake; };
  return { fake, spawn, calls };
}

const withWindow = (p, label) => Promise.race([
  p,
  new Promise((_, reject) => setTimeout(
    () => reject(new Error(
      `${label} did not resolve within ${RESOLVE_WINDOW_MS}ms (deadline ` +
      "semantics missing)")), RESOLVE_WINDOW_MS)),
]);

async function expectEscalation(fake, label) {
  const deadline = Date.now() + KILL_WINDOW_MS;
  while (fake.killCalls.length < 2 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 25));
  }
  assert.ok(fake.killCalls.length >= 2,
    `${label}: escalated second kill must arrive, got ${JSON.stringify(fake.killCalls)}`);
  assert.equal(fake.killCalls[1], ESCALATION_SIGNAL, label);
}

// -- runMoment --------------------------------------------------------------

test("runMoment: deadline + escalation through the shared run()", async () => {
  const s = killIgnoringSeam();
  const result = await withWindow(
    runMoment("turn-start", { spawn: s.spawn, timeoutMs: TIMEOUT_MS,
      escalateGraceMs: GRACE_MS }), "runMoment");
  assert.equal(s.calls.length, 1, "runMoment must pass opts.spawn through");
  assert.equal(result.raw.timedOut, true, JSON.stringify(result.raw));
  await expectEscalation(s.fake, "runMoment");
});

test("runMoment: normal forwarding preserves query/session/namespace/env", async () => {
  const s = wellBehavedSeam({ out: '{"moment":"turn-start","ok":true}\n' });
  const result = await runMoment("turn-start", {
    query: "probe-query", session: "sess-4242", namespace: "project:fwd",
    spawn: s.spawn, timeoutMs: 5000,
  });
  assert.equal(s.calls.length, 1);
  const [, args, opts] = s.calls[0];
  assert.ok(args.includes("--query") && args.includes("probe-query"),
    `query must forward: ${JSON.stringify(args)}`);
  assert.ok(args.includes("--session-id") && args.includes("sess-4242"),
    `session must forward: ${JSON.stringify(args)}`);
  assert.ok(args.includes("--namespace") && args.includes("project:fwd"),
    `namespace must forward: ${JSON.stringify(args)}`);
  assert.ok(args.includes("--lane") && args.includes("deepseek"));
  assert.equal(opts.env.ZMEM_HOST, "deepseek",
    "childEnv still applies (merge, never replace)");
  assert.equal(opts.env.ZMEM_NAMESPACE, "project:fwd");
  assert.deepEqual(result.envelope, { moment: "turn-start", ok: true });
  assert.equal(result.raw.code, 0);
  assert.ok(!result.raw.timedOut);
});

test("runMoment: caller env MERGES over the consumer env defaults", async () => {
  const s = wellBehavedSeam({ out: "{}\n" });
  const result = await runMoment("turn-start", {
    query: "merge-q", session: "sess-merge", namespace: "project:merge",
    spawn: s.spawn, timeoutMs: 5000,
    env: { ZMEM_PROBE_MERGED_KEY: "caller-wins" },
  });
  assert.equal(s.calls.length, 1);
  const opts = s.calls[0][2];
  assert.equal(opts.env.ZMEM_PROBE_MERGED_KEY, "caller-wins",
    "the caller's env key must survive the merge (present in the "
    + "spawned child env)");
  assert.equal(opts.env.ZMEM_HOST, "deepseek",
    "the consumer default survives the merge (childEnv ZMEM_HOST "
    + "still applies — merge, never replace)");
  assert.equal(opts.env.ZMEM_NAMESPACE, "project:merge",
    "the consumer default ZMEM_NAMESPACE survives the merge");
  assert.equal(result.raw.code, 0);
});

// -- runCapture -------------------------------------------------------------

test("runCapture: deadline + escalation through the shared run()", async () => {
  const s = killIgnoringSeam();
  const result = await withWindow(
    runCapture("tool_result", "probe body", {},
      { spawn: s.spawn, timeoutMs: TIMEOUT_MS, escalateGraceMs: GRACE_MS }),
    "runCapture");
  assert.equal(s.calls.length, 1, "runCapture must pass the trailing opts through");
  assert.equal(result.raw.timedOut, true, JSON.stringify(result.raw));
  await expectEscalation(s.fake, "runCapture");
});

test("runCapture: normal forwarding keeps meta on stdin and the shape", async () => {
  const s = wellBehavedSeam({ out: '{"ok":true,"deduped":false}\n' });
  const result = await runCapture("tool_result", "probe body text",
    { title: "probe title" }, { spawn: s.spawn, timeoutMs: 5000 });
  assert.equal(s.calls.length, 1);
  const [cmd, , ] = s.calls[0];
  assert.equal(cmd, "python");
  const payload = JSON.parse(s.fake.writes.join(""));
  assert.deepEqual(payload, { kind: "tool_result", text: "probe body text",
    title: "probe title" },
    "the stdin payload stays {kind, text, ...meta} exactly");
  assert.equal(result.code, 0);
  assert.deepEqual(result.result, { ok: true, deduped: false });
  assert.equal(result.raw.code, 0);
});

// -- runReflect -------------------------------------------------------------

test("runReflect: deadline + escalation through the shared run()", async () => {
  const s = killIgnoringSeam();
  const result = await withWindow(
    runReflect("sess-1", "user:global",
      { spawn: s.spawn, timeoutMs: TIMEOUT_MS, escalateGraceMs: GRACE_MS }),
    "runReflect");
  assert.equal(s.calls.length, 1, "runReflect must pass the trailing opts through");
  assert.equal(result.raw.timedOut, true, JSON.stringify(result.raw));
  await expectEscalation(s.fake, "runReflect");
});

test("runReflect: normal forwarding keeps bash argv, env merge, shape", async () => {
  const s = wellBehavedSeam({ out: "reflect-ok\n" });
  const result = await runReflect("sess-9", "project:fwd",
    { spawn: s.spawn, timeoutMs: 5000 });
  assert.equal(s.calls.length, 1);
  const [cmd, args, opts] = s.calls[0];
  assert.equal(cmd, "bash");
  assert.ok(String(args[0]).endsWith("zmem-reflect.sh"),
    `the reflect hook argv must be preserved: ${JSON.stringify(args)}`);
  assert.equal(opts.env.ZMEM_NAMESPACE, "project:fwd",
    "childEnv({ZMEM_NAMESPACE}) still applies");
  assert.equal(opts.env.ZMEM_SESSION, "sess-9");
  assert.ok(result.ok);
  assert.equal(result.out, "reflect-ok\n");
  assert.equal(result.raw.code, 0);
});

test("runReflect: caller env MERGES over the consumer env defaults", async () => {
  const s = wellBehavedSeam({ out: "reflect-ok\n" });
  const result = await runReflect("sess-merge", "project:merge",
    { spawn: s.spawn, timeoutMs: 5000,
      env: { ZMEM_PROBE_MERGED_KEY: "caller-wins" } });
  assert.equal(s.calls.length, 1);
  const opts = s.calls[0][2];
  assert.equal(opts.env.ZMEM_PROBE_MERGED_KEY, "caller-wins",
    "the caller's env key must survive the merge (present in the "
    + "spawned child env)");
  assert.equal(opts.env.ZMEM_HOST, "deepseek",
    "the consumer default survives the merge (childEnv ZMEM_HOST "
    + "still applies — merge, never replace)");
  assert.equal(opts.env.ZMEM_SESSION, "sess-merge",
    "the consumer default ZMEM_SESSION survives the merge");
  assert.ok(result.ok);
});

// -- runPolicy --------------------------------------------------------------

test("runPolicy: deadline + escalation, fail-open to empty text", async () => {
  const s = killIgnoringSeam();
  const result = await withWindow(
    runPolicy({ spawn: s.spawn, timeoutMs: TIMEOUT_MS,
      escalateGraceMs: GRACE_MS }), "runPolicy");
  assert.equal(s.calls.length, 1, "runPolicy must pass opts.spawn through");
  assert.equal(result.text, "", "runPolicy fail-opens on a timed-out helper");
  await expectEscalation(s.fake, "runPolicy");
});

test("runPolicy: normal forwarding keeps the minimal resolver env and shape", async () => {
  const s = wellBehavedSeam({ out: "# policy probe text\n" });
  const result = await runPolicy({ spawn: s.spawn, timeoutMs: 5000 });
  assert.equal(s.calls.length, 1);
  const [cmd, args, opts] = s.calls[0];
  assert.equal(cmd, "python");
  assert.ok(args.includes("--text"));
  assert.ok(Object.prototype.hasOwnProperty.call(opts.env, "ZMEM_STORE"),
    "the resolver env still carries ZMEM_STORE");
  assert.ok(!Object.prototype.hasOwnProperty.call(opts.env, "ZMEM_PRINCIPAL"),
    "policyEnv stays MINIMAL: sentinels never ride along");
  assert.equal(result.text, "# policy probe text");
  assert.equal(result.reason, "ok");
});

test("runPolicy: caller env MERGES over the minimal resolver env", async () => {
  const s = wellBehavedSeam({ out: "# policy merge probe\n" });
  const result = await runPolicy({ spawn: s.spawn, timeoutMs: 5000,
    env: { ZMEM_PROBE_MERGED_KEY: "caller-wins" } });
  assert.equal(s.calls.length, 1);
  const opts = s.calls[0][2];
  assert.equal(opts.env.ZMEM_PROBE_MERGED_KEY, "caller-wins",
    "the caller's env key must survive the merge (present in the "
    + "spawned child env)");
  assert.ok(Object.prototype.hasOwnProperty.call(opts.env, "ZMEM_STORE"),
    "the minimal resolver defaults survive the merge (ZMEM_STORE "
    + "still rides)");
  assert.ok(!Object.prototype.hasOwnProperty.call(opts.env, "ZMEM_PRINCIPAL"),
    "the minimal-env exclusions survive the merge (sentinels never "
    + "ride along)");
  assert.equal(result.text, "# policy merge probe");
  assert.equal(result.reason, "ok");
});
