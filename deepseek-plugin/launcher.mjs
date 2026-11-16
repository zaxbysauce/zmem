// ZMem DeepSeek adapter backend bridge (issues #374/#375).
// The ONLY seam to the zmem backend: spawns the same Python entrypoints the
// other hosts use. Passive moments ride the T-05 prefetch/prefetch-ack CLI
// (reserve WITHOUT committing; ack only after observed admission); explicit
// tools ride the typed op queue (ops-enqueue + ops-flush --local) and the
// read CLIs; capture rides hooks/lib/zmem-capture-entry.py. Never SQLite,
// never store.py internals, never a second store.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = path.dirname(fileURLToPath(import.meta.url));
import fs from "node:fs";

// Root resolution order: ZMEM_ROOT env (explicit) > the install-time
// pointer (zmem-root.json, written by install.mjs from the SOURCE plugin
// dir's parent — the installed profile dir contains only this package, so
// HERE/.. cannot supply skills/memory/scripts) > the repo/dev sibling
// layout (HERE/..).
function resolveRoot() {
  if (process.env.ZMEM_ROOT) return path.resolve(process.env.ZMEM_ROOT);
  const hasBackend = (candidate) => fs.existsSync(
    path.join(candidate, "skills", "memory", "scripts", "store.py"));
  try {
    const pointer = JSON.parse(fs.readFileSync(
      path.join(HERE, "zmem-root.json"), "utf8"));
    if (pointer && pointer.root && hasBackend(pointer.root)) {
      return path.resolve(pointer.root);
    }
  } catch { /* no pointer or unreadable: fall through */ }
  return path.resolve(HERE, "..");
}
export const ROOT = resolveRoot();
export const STORE_PY = path.join(ROOT, "skills", "memory", "scripts", "store.py");
const CAPTURE_PY = path.join(ROOT, "hooks", "lib", "zmem-capture-entry.py");
// Knowledge routing policy helper (docs/KNOWLEDGE-ROUTING.md): the
// standalone display-only renderer at knowledge/policy.py. Invoked ONLY
// as a bounded subprocess with NO config path — the helper's
// resolve_config_path(environ, home) is the single common authority for
// the canonical store config (mirroring the host store chain,
// file-shaped refusals included), resolved from the MINIMAL resolver env
// passed below. Never imported, never given credentials, never a model
// call, and this adapter never duplicates the resolution chain.
const POLICY_PY = path.join(
  ROOT, "skills", "memory", "scripts", "knowledge", "policy.py");

// Minimal child env for the policy helper (FB-028). The helper resolves
// the canonical store config CLIENT-SIDE, so it gets exactly the resolver
// variables — ZMEM_STORE included even in remote mode: the childEnv()
// "delete ZMEM_STORE under ZMEM_REMOTE_URL" normalization exists for
// STORE access (no local store in remote mode) and must NOT reach the
// policy child, whose config resolution ranks ZMEM_STORE first. Beyond
// the resolver variables only interpreter/system basics ride along;
// credentials, principals, and every other sentinel env var stay in this
// process (the helper never reads them). ZMEM_TEST_* is the test-harness
// driver seam (stub helpers), not a credential lane.
const POLICY_ENV_KEYS = [
  "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
  "ZMEM_HOME", "ZMEM_REMOTE_URL",
  "HOME", "USERPROFILE", "USERNAME",
  "SystemRoot", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP",
  "PYTHONIOENCODING",
];
function policyEnv() {
  const env = {};
  for (const key of POLICY_ENV_KEYS) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  for (const key of Object.keys(process.env)) {
    if (key.startsWith("ZMEM_TEST_")) env[key] = process.env[key];
  }
  return env;
}

export function childEnv(extra = {}) {
  const env = { ...process.env, ...extra };
  // Normalization (plan D1-prime): pin ZMEM_DATA from the plugin-data env
  // so the launcher's ~/.zmem default can never engage on a broker box;
  // dual kill switches; never leak an explicit store in remote mode.
  for (const key of ["ZCODE_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"]) {
    if (env[key] && !env.ZMEM_DATA) env.ZMEM_DATA = env[key];
  }
  if ((env.ZMEM_REMOTE_URL || "").trim()) delete env.ZMEM_STORE;
  env.ZMEM_HOST = env.ZMEM_HOST || "deepseek";
  return env;
}

function run(argv, opts = {}) {
  // Settlement law (#481 AC1/AC9): the deadline timer resolves the promise
  // IMMEDIATELY with the output collected so far — resolution never waits
  // for the child.  After resolving, best-effort background cleanup: a
  // graceful kill at the deadline, escalation to SIGKILL after
  // opts.escalateGraceMs (default 2000) if the child still has not closed;
  // every kill is wrapped so a throwing kill never crashes the plugin; all
  // timers are cleared on any settlement and listeners detached so late
  // error/close events no-op (one-shot settlement).  A child closing during
  // the grace does not alter the already-resolved result.
  const spawnFn = opts.spawn || spawn;
  return new Promise((resolve) => {
    let settled = false;
    let out = "";
    let err = "";
    let hardTimer = null;
    const child = spawnFn(argv[0], argv.slice(1), {
      env: opts.env || childEnv(),
      cwd: opts.cwd || ROOT,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    const killSafe = (signal) => {
      try { return child.kill(signal); } catch { return false; }
    };
    const settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (hardTimer) clearTimeout(hardTimer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      // Deadline: resolve immediately with the collected output.
      settle({ code: null, out, err, timedOut: true });
      // Best-effort cleanup, never affecting the settled result.  Listeners
      // stay attached — the settled guard makes late events no-op (removing
      // the error listener would make a late child error THROW as an
      // uncaught exception, which is its own crash class).
      killSafe();
      const grace = opts.escalateGraceMs ?? 2000;
      hardTimer = setTimeout(() => { killSafe("SIGKILL"); }, grace);
    }, opts.timeoutMs || 20000);
    child.stdout.on("data", (d) => { if (!settled) out += d; });
    child.stderr.on("data", (d) => { if (!settled) err += d; });
    child.on("error", () => { if (hardTimer) clearTimeout(hardTimer); settle({ code: 1, out, err }); });
    child.on("close", (code) => { if (hardTimer) clearTimeout(hardTimer); settle({ code: code ?? 1, out, err }); });
    if (opts.stdin) { child.stdin.write(opts.stdin); }
    child.stdin.end();
  });
}

function lastJson(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return null;
  try { return JSON.parse(trimmed); } catch { /* multi-doc output below */ }
  for (const line of trimmed.split(/\r?\n/).reverse()) {
    try { return JSON.parse(line); } catch { /* keep scanning */ }
  }
  return null;
}

// Passive moment through the T-05 CLI seam (both modes; remote_gate routes).
export async function runMoment(moment, { query = "", session, namespace, cwd, ...opts } = {}) {
  const args = [process.execPath.startsWith("python") ? process.execPath : "python",
    "-X", "utf8", STORE_PY, "prefetch", "--reserve", "--json",
    "--query", query || "", "--namespace", namespace || "user:global",
    "--moment", moment, "--lane", "deepseek",
    "--session-id", session || ""];
  if (process.env.ZMEM_PRINCIPAL && !(process.env.ZMEM_REMOTE_URL || "").trim()) {
    args.push("--principal", process.env.ZMEM_PRINCIPAL, "--harness", "deepseek");
  }
  const r = await run(args, { timeoutMs: 20000, env: childEnv({ ZMEM_NAMESPACE: namespace || "" }), cwd: cwd || ROOT, ...opts });
  return { envelope: lastJson(r.out), raw: r };
}

// Acknowledge a reservation AFTER observed admission, SYNCHRONOUSLY and
// bounded (the dsh session/event listener is emit-mode — an async handler's
// subprocess can outlive the runtime and the durable ledger write would be
// lost; the hook hosts use the same blocking-at-the-moment shape).
export function ackReservationSync(reservationId, timeoutMs = 12000) {
  if (!reservationId) return { ok: true };
  const { spawnSync } = require("node:child_process");
  const r = spawnSync("python", ["-X", "utf8", STORE_PY, "prefetch-ack",
    "--reservation", reservationId],
    { env: childEnv(), cwd: ROOT, timeout: timeoutMs,
      windowsHide: true });
  return { ok: r.status === 0 };
}

// Explicit tools: reads via the CLI (remote_gate routes), writes via the
// typed queue (pending receipt remote; committed receipt local).
export async function runTool(cliArgs, opts = {}) {
  const r = await run(["python", "-X", "utf8", STORE_PY, ...cliArgs],
    { timeoutMs: 30000, ...opts });
  return { code: r.code, result: lastJson(r.out), raw: r };
}

export async function enqueueOp(op, argsJson, dedupKey, opts = {}) {
  const argv = ["python", "-X", "utf8", STORE_PY, "ops-enqueue",
    "--op", op, "--args", JSON.stringify(argsJson), "--json"];
  if (dedupKey) argv.push("--dedup-key", dedupKey);
  const r = await run(argv, { timeoutMs: 15000, ...opts });
  return { code: r.code, receipt: lastJson(r.out), raw: r };
}

export async function flushLocal(opts = {}) {
  const r = await run(["python", "-X", "utf8", STORE_PY, "ops-flush",
    "--local", "--json"], { timeoutMs: 60000, ...opts });
  return { code: r.code, receipts: lastJson(r.out), raw: r };
}

// Capture through the shared python entry (always-on redaction, dedup).
export async function runCapture(kind, text, meta = {}, opts = {}) {
  const payload = JSON.stringify({ kind, text, ...meta });
  const r = await run(["python", "-X", "utf8", CAPTURE_PY],
    { timeoutMs: 15000, stdin: payload, ...opts });
  return { code: r.code, result: lastJson(r.out), raw: r };
}

// The shared reflect hook (bounded nudge; its own eligibility governs).
export async function runReflect(session, namespace, opts = {}) {
  const env = childEnv({ ZMEM_NAMESPACE: namespace || "", ZMEM_SESSION: session || "" });
  const r = await run(["bash", path.join(ROOT, "hooks", "zmem-reflect.sh")],
    { timeoutMs: 12000, env, stdin: JSON.stringify({ session_id: session || "" }), ...opts });
  return { ok: r.code === 0, out: r.out, raw: r };
}

// Knowledge routing policy: render the canonical concise policy via the
// standalone helper, FROZEN seam: CLI --text|--json with an OPTIONAL
// --config and a --no-capture flag (render_policy(config_path=None,
// environ=None, *, allow_capture=True) — allow_capture keyword-only —
// on the API side). This adapter passes
// NO config path — the helper resolves the canonical store config from
// the minimal resolver env (policyEnv() above) — and maps ZMEM_CAPTURE=0
// to --no-capture so the helper renders read-routing WITHOUT a capture
// invitation. The CALLER owns the ZMEM_INJECT=0 gate (no spawn at all
// under it). Cheap by contract — no model, no store, no network, no root
// stat — and time-bounded by the kill timer in run(); the bound is
// run() enforces a HARD deadline (#481 F-008): the promise resolves ON
// the timeout and escalates to SIGKILL after a grace; a helper that
// ignores the first kill can no longer stall the pre-step.
// Fail-open everywhere: absent helper (checkouts without it), non-zero
// exit, timeout, or empty output (the companion's default-off state) all
// yield { text: "" } and the caller appends nothing.
export async function runPolicy({ timeoutMs = 5000, ...opts } = {}) {
  try {
    if (!fs.existsSync(POLICY_PY)) {
      return { text: "", reason: "helper-absent" };
    }
  } catch {
    return { text: "", reason: "helper-absent" };
  }
  const captureOff = (process.env.ZMEM_CAPTURE || "").trim() === "0";
  const argv = ["python", "-X", "utf8", POLICY_PY, "--text"];
  if (captureOff) argv.push("--no-capture");
  const r = await run(argv, { timeoutMs, env: policyEnv(), ...opts });
  if (r.code !== 0) return { text: "", reason: "exit-" + r.code };
  const text = (r.out || "").trim();
  if (!text) return { text: "", reason: "disabled" };
  return { text: text.slice(0, 8192), reason: "ok" };
}

export { run, lastJson };
