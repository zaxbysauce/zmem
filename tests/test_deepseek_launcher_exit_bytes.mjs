// Arm's-length acceptance check: deepseek-plugin/launcher.mjs preserves
// exit codes/stdout/stderr bytes under the injected-spawn seam AND the
// default real-spawn path, including the typed-queue consumers' DEFAULT
// option shapes (issue trace 480-481-482, Fix 3, rebuild r2).
//
// Frozen seam contract:
//   - `run(argv, opts)`: `opts.spawn` injectable; default real spawn
//     unchanged; well-behaved helper resolves EXACTLY
//     { code: 7, out: <stdout bytes>, err: <stderr bytes> }.
//   - The typed-queue consumers keep their EXISTING signatures and gain
//     ONLY an optional trailing opts passed through to run():
//       runTool(cliArgs, opts?)          (existing callers: runTool(args))
//       enqueueOp(op, argsJson, dedupKey?, opts?)
//       flushLocal(opts?)
//     with opts `{ spawn?, timeoutMs?, escalateGraceMs? }`.
//
// Default-preservation legs pin TODAY's behavior: they spawn the REAL
// python store.py against a scratch store (ZMEM_STORE/ZMEM_DATA pinned
// below before importing the launcher), so they stay green at head only
// if the defaults are untouched.  Base: the injected legs are RED
// (opts.spawn ignored); the default legs are GREEN.
//
// Run: node --test tests/test_deepseek_launcher_exit_bytes.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import {
  STORE_PY,
  enqueueOp,
  flushLocal,
  run,
  runTool,
} from "../deepseek-plugin/launcher.mjs";

// Scratch env pinned BEFORE the import above takes effect for children:
// the default legs must never touch a real store.
const SCRATCH = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-exitbytes-"));
process.env.ZMEM_STORE = path.join(SCRATCH, "store.sqlite");
process.env.ZMEM_DATA = SCRATCH;
process.env.ZMEM_MODEL_AUTODOWNLOAD = "0";
process.env.ZMEM_MODELS_DIR = path.join(SCRATCH, "no-models");
delete process.env.ZMEM_REMOTE_URL;
// Initialize the scratch store so the real store.py legs succeed.
execFileSync("python", ["-X", "utf8", STORE_PY, "stats"],
  { stdio: "ignore", env: process.env, windowsHide: true });

const OUT_BYTES = "OUT7-stdout-bytes\n";
const ERR_BYTES = "ERR7-stderr-bytes\n";

// Minimal fake stdio: EventEmitter plus the setEncoding no-op run() now
// calls on real child streams right after spawn (FB-016b). The fakes emit
// complete-ASCII Buffers, so the no-op changes no existing leg's bytes.
class FakeStdio extends EventEmitter {
  setEncoding() {}
}

class ExitSevenChild extends EventEmitter {
  constructor() {
    super();
    this.stdout = new FakeStdio();
    this.stderr = new FakeStdio();
    this.stdin = { write() {}, end() {} };
    this.killCalls = [];
  }
  kill(signal) { this.killCalls.push(signal ?? null); return true; }
  async script() {
    await new Promise((r) => setImmediate(r));
    this.stdout.emit("data", Buffer.from(OUT_BYTES, "utf8"));
    this.stderr.emit("data", Buffer.from(ERR_BYTES, "utf8"));
    this.emit("close", 7, null);
  }
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

test("injected leg: exit 7 with known bytes resolves byte-for-byte", async () => {
  const fake = new ExitSevenChild();
  const spawnCalls = [];
  const spawn = (cmd, args, opts) => { spawnCalls.push([cmd, args]); fake.script(); return fake; };
  const result = await run(["arm-length-probe-exit-seven"], {
    timeoutMs: 5000, spawn,
  });
  assert.equal(spawnCalls.length, 1,
    "opts.spawn must be used instead of the real child_process.spawn");
  assert.equal(result.code, 7, JSON.stringify(result));
  assert.equal(result.out, OUT_BYTES, JSON.stringify(result));
  assert.equal(result.err, ERR_BYTES, JSON.stringify(result));
  assert.ok(!result.timedOut, "a clean exit-7 is not a timeout");
  assert.equal(fake.killCalls.length, 0,
    "a well-behaved helper inside the window is never killed");
});

test("real-spawn leg: exit 7 with known bytes resolves byte-for-byte", async () => {
  const helper = [
    `process.stdout.write(${JSON.stringify(OUT_BYTES)});`,
    `process.stderr.write(${JSON.stringify(ERR_BYTES)});`,
    "process.exit(7);",
  ].join(" ");
  const result = await run([process.execPath, "-e", helper], { timeoutMs: 5000 });
  assert.equal(result.code, 7, JSON.stringify(result));
  assert.equal(result.out, OUT_BYTES, JSON.stringify(result));
  assert.equal(result.err, ERR_BYTES, JSON.stringify(result));
  assert.ok(!result.timedOut);
});

test("default leg: runTool(cliArgs) keeps its shape on a real spawn", async () => {
  // The exact argv the real zmem_recent tool uses today.
  const r = await runTool(["recent", "--json"]);
  assert.equal(typeof r.code, "number", JSON.stringify(r));
  assert.equal(r.code, 0, `recent must succeed on the scratch store: ${JSON.stringify(r.raw)}`);
  assert.ok(r.result && typeof r.result === "object",
    "runTool.result stays the parsed last-JSON of stdout");
  assert.equal(r.raw.code, 0);
  assert.equal(typeof r.raw.out, "string");
  assert.ok(!r.raw.timedOut);
});

test("default leg: flushLocal() keeps its shape on a real spawn", async () => {
  const r = await flushLocal();
  assert.equal(r.code, 0, `ops-flush must succeed on the empty scratch store: ${JSON.stringify(r.raw)}`);
  assert.ok(r.receipts && typeof r.receipts === "object",
    "flushLocal.receipts stays the parsed last-JSON of stdout");
  assert.equal(r.raw.code, 0);
});

test("default leg: enqueueOp keeps its shape on a real spawn", async () => {
  const r = await enqueueOp("add", { namespace: "user:global", text: "exit-bytes probe" });
  assert.equal(typeof r.code, "number", JSON.stringify(r));
  assert.equal(typeof r.raw.out, "string");
  assert.equal(typeof r.raw.err, "string");
  // Whatever the verdict, the SURFACE keeps {code, receipt, raw}.
  assert.deepEqual(Object.keys(r).sort(), ["code", "raw", "receipt"]);
});

test("deadline leg: the typed-queue consumers inherit the deadline via trailing opts", async () => {
  const mk = () => {
    const fake = new KillIgnoringChild();
    const calls = [];
    const spawn = (cmd, args, opts) => { calls.push([cmd, args]); return fake; };
    return { fake, spawn, calls };
  };
  const windowMs = 900;
  const withWindow = (p) => Promise.race([
    p,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error("did not resolve within the deadline window")),
      windowMs)),
  ]);

  const tool = mk();
  const toolR = await withWindow(runTool(["recent", "--json"], {
    timeoutMs: 150, escalateGraceMs: 100, spawn: tool.spawn }));
  assert.equal(tool.calls.length, 1,
    "runTool(cliArgs, opts) must route opts.spawn through to run()");
  assert.equal(toolR.raw.timedOut, true, JSON.stringify(toolR.raw));

  const enq = mk();
  const enqR = await withWindow(enqueueOp("add", { text: "probe" }, null, {
    timeoutMs: 150, escalateGraceMs: 100, spawn: enq.spawn }));
  assert.equal(enq.calls.length, 1,
    "enqueueOp must accept a trailing opts passed through to run()");
  assert.equal(enqR.raw.timedOut, true, JSON.stringify(enqR.raw));

  const flush = mk();
  const flushR = await withWindow(flushLocal({
    timeoutMs: 150, escalateGraceMs: 100, spawn: flush.spawn }));
  assert.equal(flush.calls.length, 1,
    "flushLocal(opts) must pass opts through to run()");
  assert.equal(flushR.raw.timedOut, true, JSON.stringify(flushR.raw));
});

test("utf8 leg: a multibyte character split across chunks is not corrupted (FB-016b)", async () => {
  // The helper writes the 4 UTF-8 bytes of U+1F600 as TWO separate
  // stdout writes (2 bytes + 2 bytes) with a delay between them, so the
  // parent receives two chunks that each end MID-CHARACTER; stderr gets
  // the same treatment. Without setEncoding("utf8") on the child streams,
  // `out += d` stringifies each Buffer chunk in isolation and every split
  // byte pair corrupts to U+FFFD. The process exits naturally after the
  // second write (no process.exit, which can truncate pending pipe
  // writes).
  const CH = "\u{1F600}";
  const bytes = Buffer.from(CH, "utf8");
  assert.equal(bytes.length, 4, "the probe character is 4 UTF-8 bytes");
  const helper = [
    `const b = Buffer.from("${CH}", "utf8");`,
    "process.stdout.write(b.subarray(0, 2));",
    "process.stderr.write(b.subarray(0, 2));",
    "setTimeout(() => {",
    "  process.stdout.write(b.subarray(2));",
    "  process.stderr.write(b.subarray(2));",
    "}, 60);",
  ].join(" ");
  const result = await run([process.execPath, "-e", helper], { timeoutMs: 5000 });
  assert.equal(result.code, 0, JSON.stringify(result));
  assert.equal(result.out, CH,
    `stdout must decode the split multibyte character exactly, got `
    + `${JSON.stringify(result.out)} (U+FFFD corruption means the child `
    + `stream lacks utf8 decoding at the boundary)`);
  assert.equal(result.err, CH,
    `stderr must decode the split multibyte character exactly, got `
    + `${JSON.stringify(result.err)}`);
  assert.ok(!result.timedOut);
});
