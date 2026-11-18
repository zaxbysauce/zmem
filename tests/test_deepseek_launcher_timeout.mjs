// Arm's-length acceptance checks: deepseek-plugin/launcher.mjs `run()`
// timeout SETTLEMENT LAW (issue trace 480-481-482, Fix 3, rebuild r3).
//
// FROZEN SETTLEMENT LAW (one law; the implementer must satisfy exactly):
//   - The timer firing at `opts.timeoutMs` resolves the promise
//     IMMEDIATELY with `{ code: null, out: <collected so far>,
//     err: <collected so far>, timedOut: true }`.  Resolution NEVER
//     waits for the child: a close during the grace window may NOT alter
//     the already-resolved result.
//   - After resolving, best-effort BACKGROUND cleanup: graceful kill at
//     the deadline; escalate to `kill("SIGKILL")` after
//     `opts.escalateGraceMs` (default 2000) if the child still has not
//     closed; every kill call is wrapped so a THROWING kill never
//     crashes; all timers are cleared on any settlement; listeners are
//     removed after settlement (late error/close events are no-ops).
//   - The well-behaved path is unchanged: byte-for-byte code/out/err,
//     `timedOut` falsy, zero kills.
//
// Base: legs (i)-(iv),(vi) are RED (opts.spawn / escalateGraceMs seam
// missing; the real spawn resolves by child events with a non-null code);
// leg (vii) real-spawn is GREEN at base.
//
// Run: node --test tests/test_deepseek_launcher_timeout.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { run } from "../deepseek-plugin/launcher.mjs";

const TIMEOUT_MS = 150;
const GRACE_MS = 100;
const SLACK_MS = 350;                    // timer-scheduling slack
const SETTLE_WINDOW_MS = TIMEOUT_MS + SLACK_MS;   // resolve ON the deadline
const KILL_WINDOW_MS = TIMEOUT_MS + GRACE_MS + 2000;
const ESCALATION_SIGNAL = "SIGKILL";

// Minimal fake stdio: EventEmitter plus the setEncoding no-op run() now
// calls on real child streams right after spawn (FB-016b). The fakes emit
// complete-ASCII Buffers, so the no-op changes no existing leg's bytes.
class FakeStdio extends EventEmitter {
  setEncoding() {}
}

class FakeChild extends EventEmitter {
  constructor({ killBehavior = "ignore", closeAfterMs = 60,
                throwOnKill = false, exitCode = 8 } = {}) {
    super();
    this.stdout = new FakeStdio();
    this.stderr = new FakeStdio();
    this.stdin = { write() {}, end() {} };
    this.killCalls = [];
    this.closed = false;
    this._killBehavior = killBehavior;
    this._closeAfterMs = closeAfterMs;
    this._throwOnKill = throwOnKill;
    this._exitCode = exitCode;
    this._closeTimer = null;
  }

  kill(signal) {
    this.killCalls.push(signal ?? null);
    if (this._throwOnKill) {
      throw new Error("kill refused (probe)");
    }
    if (this.killCalls.length >= 2) {
      this._finish();
      return true;
    }
    if (this._killBehavior === "close-soon" && this._closeTimer === null) {
      this._closeTimer = setTimeout(() => this._finish(),
                                     this._closeAfterMs);
    }
    return true;
  }

  _finish() {
    if (this.closed) return;
    this.closed = true;
    if (this._closeTimer !== null) clearTimeout(this._closeTimer);
    this.emit("close", this._exitCode, null);
  }
}

function seam(ctor = {}) {
  const fake = new FakeChild(ctor);
  const spawnCalls = [];
  const spawn = (cmd, args, opts) => {
    spawnCalls.push([cmd, args, opts]);
    return fake;
  };
  return { fake, spawn, spawnCalls };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const timeouts = () => {
  const counts = {};
  for (const name of process.getActiveResourcesInfo()) {
    counts[name] = (counts[name] || 0) + 1;
  }
  return counts;
};

test("(i) kill-ignoring child: resolves ON the deadline, code null, escalated SIGKILL", async () => {
  const s = seam({ killBehavior: "ignore" });
  const startedAt = Date.now();
  const result = await run(["arm-length-probe-helper"], {
    timeoutMs: TIMEOUT_MS, escalateGraceMs: GRACE_MS, spawn: s.spawn,
  });
  const elapsed = Date.now() - startedAt;
  assert.equal(s.spawnCalls.length, 1,
    "opts.spawn must be used instead of the real child_process.spawn");
  assert.ok(elapsed >= TIMEOUT_MS - 30 && elapsed <= SETTLE_WINDOW_MS,
    `resolution must be ON the deadline (deadline semantics), got `
    + `${elapsed}ms (window [${TIMEOUT_MS - 30}, ${SETTLE_WINDOW_MS}])`);
  assert.equal(result.timedOut, true, JSON.stringify(result));
  assert.equal(result.code, null,
    `the deadline result carries code null (no child verdict): `
    + JSON.stringify(result));
  assert.equal(result.out, "");
  assert.equal(result.err, "");
  const deadline = Date.now() + KILL_WINDOW_MS;
  while (s.fake.killCalls.length < 2 && Date.now() < deadline) {
    await sleep(25);
  }
  assert.ok(s.fake.killCalls.length >= 2,
    `escalated second kill must arrive, got `
    + JSON.stringify(s.fake.killCalls));
  assert.equal(s.fake.killCalls[1], ESCALATION_SIGNAL);
});

test("(ii) close during grace: resolves ON the deadline anyway; close may NOT alter the result", async () => {
  const s = seam({ killBehavior: "close-soon", closeAfterMs: 50,
                   exitCode: 8 });
  const startedAt = Date.now();
  const result = await run(["arm-length-probe-grace-close"], {
    timeoutMs: TIMEOUT_MS, escalateGraceMs: GRACE_MS, spawn: s.spawn,
  });
  const elapsed = Date.now() - startedAt;
  assert.equal(s.spawnCalls.length, 1);
  assert.ok(elapsed <= SETTLE_WINDOW_MS,
    `resolution must be ON the deadline, not at the close (${elapsed}ms)`);
  assert.equal(result.timedOut, true, JSON.stringify(result));
  assert.equal(result.code, null,
    `the deadline result carries code null even when the child closes `
    + `during the grace window: ${JSON.stringify(result)}`);
  // The child closed during the grace: the background cleanup sees a
  // closed child and must NOT escalate.
  await sleep(GRACE_MS + 200);
  assert.equal(s.fake.killCalls.length, 1,
    `no escalated kill after a close during grace, got `
    + JSON.stringify(s.fake.killCalls));
});

test("(iii) kill() throws on BOTH calls: both attempts recorded, still resolves on the deadline", async () => {
  const s = seam({ throwOnKill: true });
  const startedAt = Date.now();
  const result = await run(["arm-length-probe-throwing-kill"], {
    timeoutMs: TIMEOUT_MS, escalateGraceMs: GRACE_MS, spawn: s.spawn,
  });
  const elapsed = Date.now() - startedAt;
  assert.equal(s.spawnCalls.length, 1);
  assert.ok(elapsed <= SETTLE_WINDOW_MS + 200,
    `throwing kills must never hang resolution (${elapsed}ms)`);
  // AWAIT BOTH kill attempts: let the grace fully elapse and the
  // escalation fire (and throw) before asserting resolution.
  const bothDeadline = Date.now() + KILL_WINDOW_MS;
  while (s.fake.killCalls.length < 2 && Date.now() < bothDeadline) {
    await sleep(25);
  }
  assert.equal(s.fake.killCalls.length, 2,
    `BOTH kill attempts must be recorded (graceful + escalated), got `
    + JSON.stringify(s.fake.killCalls));
  assert.equal(result.timedOut, true,
    `throwing kills still settle as timeout: ${JSON.stringify(result)}`);
  assert.equal(result.code, null);
});

test("(iv) single settlement: AWAITED late error/close events change nothing", async () => {
  const s = seam({ killBehavior: "ignore" });
  const result = await run(["arm-length-probe-late-events"], {
    timeoutMs: TIMEOUT_MS, escalateGraceMs: GRACE_MS, spawn: s.spawn,
  });
  const settled = JSON.parse(JSON.stringify(result));
  // Wait for the background cleanup (and any dangling listener window)
  // to fully pass BEFORE firing the late events, then await them.
  await sleep(GRACE_MS + 300);
  s.fake.stdout.emit("data", Buffer.from("late-out\n", "utf8"));
  s.fake.stderr.emit("data", Buffer.from("late-err\n", "utf8"));
  s.fake.emit("error", new Error("late error after settlement"));
  s.fake.emit("close", 0, null);
  await sleep(150);   // properly awaited before asserting
  assert.deepEqual(JSON.parse(JSON.stringify(result)), settled,
    "late events after settlement must not change the resolved result");
});

test("(v) timer cleanup: the close handler clears the escalation timer before the grace elapses", async () => {
  const s = seam({ killBehavior: "close-soon", closeAfterMs: 30 });
  const baseline = timeouts()["Timeout"] || 0;
  const pending = run(["arm-length-probe-timer-cleanup"], {
    timeoutMs: TIMEOUT_MS, escalateGraceMs: 800, spawn: s.spawn,
  });
  // The fake closes closeAfterMs after the deadline's graceful kill —
  // poll for the close (5ms interval, 400ms budget) so the handle
  // sample below lands right after the close and BEFORE the 800ms
  // grace elapses.
  const closeDeadline = Date.now() + 400;
  while (!s.fake.closed && Date.now() < closeDeadline) {
    await sleep(5);
  }
  assert.ok(s.fake.closed,
    "the fake child must close during the grace window (close-soon "
    + "seam, closeAfterMs 30)");
  // SAMPLE now, before the grace elapses: the close handler must
  // already have cleared the escalation hardTimer — a mutant that
  // leaves it armed holds a live Timeout handle for the full grace.
  const after = timeouts()["Timeout"] || 0;
  assert.ok(after <= baseline,
    `the close handler must clear the escalation timer before the `
    + `grace elapses (baseline Timeout handles ${baseline}, `
    + `after ${after})`);
  const result = await pending;
  assert.equal(s.spawnCalls.length, 1,
    "opts.spawn must be used instead of the real child_process.spawn");
  assert.equal(result.timedOut, true, JSON.stringify(result));
  assert.equal(result.code, null,
    `the deadline result carries code null (no child verdict): `
    + JSON.stringify(result));
});

test("(vi) partial output: the deadline result carries exactly the bytes collected", async () => {
  const s = seam({ killBehavior: "ignore" });
  const spawnCalls = [];
  const spawn = (cmd, args, opts) => {
    spawnCalls.push([cmd, args]);
    // ASYNCHRONOUS emission: queue the data events AFTER spawn returns
    // (setImmediate) so run()'s listeners attach first — a synchronous
    // emit inside spawn() is unrealizable by normal wiring.
    setImmediate(() => {
      s.fake.stdout.emit("data", Buffer.from("PARTIAL-OUT\n", "utf8"));
      s.fake.stderr.emit("data", Buffer.from("PARTIAL-ERR\n", "utf8"));
    });
    return s.fake;
  };
  const result = await run(["arm-length-probe-partial"], {
    timeoutMs: TIMEOUT_MS, escalateGraceMs: GRACE_MS, spawn,
  });
  assert.equal(spawnCalls.length, 1);
  assert.equal(result.timedOut, true);
  assert.equal(result.code, null);
  assert.equal(result.out, "PARTIAL-OUT\n",
    `out carries exactly the pre-deadline bytes: ${JSON.stringify(result)}`);
  assert.equal(result.err, "PARTIAL-ERR\n",
    `err carries exactly the pre-deadline bytes: ${JSON.stringify(result)}`);
  // Drain the background escalation timer (the kill-ignoring child never
  // closes, so the hardTimer stays armed past this test's asserts): later
  // legs sample live Timeout handles, and a timer firing inside their
  // sample window would shift those counts.
  await sleep(GRACE_MS + 400);
});

test("(vii) well-behaved helper keeps exit code and byte-for-byte out/err", async () => {
  const result = await run(
    [process.execPath, "-e", "console.log('ok')"],
    { timeoutMs: 5000 });
  assert.equal(result.code, 0, JSON.stringify(result));
  assert.equal(result.out, "ok\n", JSON.stringify(result));
  assert.equal(result.err, "", JSON.stringify(result));
  assert.ok(!result.timedOut, "a clean exit is not a timeout");
});

test("(viii) well-behaved real spawn: settle clears the deadline timer immediately", async () => {
  const baseline = timeouts()["Timeout"] || 0;
  const result = await run(
    [process.execPath, "-e", "console.log('ok')"],
    { timeoutMs: 5000 });
  // Sample IMMEDIATELY after resolve: the child settled through the
  // close handler long before the deadline, and settle() must already
  // have cleared the 5000ms deadline timer — a mutant that leaves it
  // armed holds a live Timeout handle for the rest of the window.
  const after = timeouts()["Timeout"] || 0;
  assert.ok(after <= baseline,
    `settlement must clear the deadline timer immediately (baseline `
    + `Timeout handles ${baseline}, after ${after})`);
  assert.equal(result.code, 0, JSON.stringify(result));
});
