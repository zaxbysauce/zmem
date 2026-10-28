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
  return new Promise((resolve) => {
    const child = spawn(argv[0], argv.slice(1), {
      env: opts.env || childEnv(),
      cwd: opts.cwd || ROOT,
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    let out = "";
    let err = "";
    const timer = setTimeout(() => child.kill(), opts.timeoutMs || 20000);
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { err += d; });
    child.on("error", () => { clearTimeout(timer); resolve({ code: 1, out, err }); });
    child.on("close", (code) => { clearTimeout(timer); resolve({ code: code ?? 1, out, err }); });
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
export async function runMoment(moment, { query = "", session, namespace, cwd }) {
  const args = [process.execPath.startsWith("python") ? process.execPath : "python",
    "-X", "utf8", STORE_PY, "prefetch", "--reserve", "--json",
    "--query", query || "", "--namespace", namespace || "user:global",
    "--moment", moment, "--lane", "deepseek",
    "--session-id", session || ""];
  if (process.env.ZMEM_PRINCIPAL && !(process.env.ZMEM_REMOTE_URL || "").trim()) {
    args.push("--principal", process.env.ZMEM_PRINCIPAL, "--harness", "deepseek");
  }
  const r = await run(args, { timeoutMs: 20000, env: childEnv({ ZMEM_NAMESPACE: namespace || "" }), cwd: cwd || ROOT });
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
export async function runTool(cliArgs) {
  const r = await run(["python", "-X", "utf8", STORE_PY, ...cliArgs],
    { timeoutMs: 30000 });
  return { code: r.code, result: lastJson(r.out), raw: r };
}

export async function enqueueOp(op, argsJson, dedupKey) {
  const argv = ["python", "-X", "utf8", STORE_PY, "ops-enqueue",
    "--op", op, "--args", JSON.stringify(argsJson), "--json"];
  if (dedupKey) argv.push("--dedup-key", dedupKey);
  const r = await run(argv, { timeoutMs: 15000 });
  return { code: r.code, receipt: lastJson(r.out), raw: r };
}

export async function flushLocal() {
  const r = await run(["python", "-X", "utf8", STORE_PY, "ops-flush",
    "--local", "--json"], { timeoutMs: 60000 });
  return { code: r.code, receipts: lastJson(r.out), raw: r };
}

// Capture through the shared python entry (always-on redaction, dedup).
export async function runCapture(kind, text, meta = {}) {
  const payload = JSON.stringify({ kind, text, ...meta });
  const r = await run(["python", "-X", "utf8", CAPTURE_PY],
    { timeoutMs: 15000, stdin: payload });
  return { code: r.code, result: lastJson(r.out), raw: r };
}

// The shared reflect hook (bounded nudge; its own eligibility governs).
export async function runReflect(session, namespace) {
  const env = childEnv({ ZMEM_NAMESPACE: namespace || "", ZMEM_SESSION: session || "" });
  const r = await run(["bash", path.join(ROOT, "hooks", "zmem-reflect.sh")],
    { timeoutMs: 12000, env, stdin: JSON.stringify({ session_id: session || "" }) });
  return { ok: r.code === 0, out: r.out, raw: r };
}

export { run, lastJson };
