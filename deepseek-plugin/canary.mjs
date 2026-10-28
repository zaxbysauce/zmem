#!/usr/bin/env node
// Native DeepSeek canary runner (issues #374 AC7 / #375 AC6).
// Usage: node canary.mjs --out <path>
//
// Boots the real pinned dsh runtime + the plugin against an ISOLATED store,
// exercises delivery/ack/tools/capture/offline/compaction legs, and writes
// the version-bound canary report (schema zmem-deepseek-canary-v1; the
// claim-gate test validates it). No model provider, no network to a model.
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const RUNTIME = process.env.ZMEM_DEEPSEEK_RUNTIME
  || path.join(HERE, "node_modules");

// The doc-drift hygiene ratchet (tests/test_doc_drift.py HOME_PATH_RE)
// forbids tracked files from embedding absolute user home paths: the
// operator username must not leak into committed evidence. Leg state (and
// anything else folded into the report) is scrubbed at report-build time —
// <HOME> is the sanctioned placeholder (angle-bracket segments never match
// the ratchet).
const HOME_DIR = process.env.USERPROFILE || process.env.HOME || "";
function scrubHome(value) {
  if (!HOME_DIR) return value;
  if (typeof value === "string") {
    let out = value.split(HOME_DIR).join("<HOME>");
    const fwd = HOME_DIR.replace(/\\/g, "/");
    if (fwd !== HOME_DIR && fwd.includes("/")) {
      out = out.split(fwd).join("<HOME>");
    }
    return out;
  }
  if (Array.isArray(value)) return value.map(scrubHome);
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, val] of Object.entries(value)) out[key] = scrubHome(val);
    return out;
  }
  return value;
}

function parseArgs() {
  const out = { out: null };
  for (let i = 2; i < process.argv.length; i++) {
    if (process.argv[i] === "--out") out.out = process.argv[++i];
  }
  return out;
}

async function runtimeVersions() {
  const pkgs = {
    "@deepseek-ai/cordis": "package.json",
    "@deepseek-ai/dsh-agent-loop": "package.json",
    "@deepseek-ai/dsh-llm": "package.json",
    "@deepseek-ai/dsh-tools": "package.json",
  };
  const out = {};
  for (const [name, file] of Object.entries(pkgs)) {
    try {
      const p = path.join(RUNTIME, name, file);
      out[name] = JSON.parse(fs.readFileSync(p, "utf8")).version;
    } catch {
      out[name] = null;
    }
  }
  return out;
}

function readManifestVersion() {
  try {
    return JSON.parse(fs.readFileSync(
      path.join(ROOT, "release-manifest.json"), "utf8")).version;
  } catch {
    return null;
  }
}

async function main() {
  const { out } = parseArgs();
  if (!out) {
    console.error("canary: --out <path> is required");
    process.exit(2);
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-ds-canary-"));
  const statePath = path.join(tmp, "state.json");
  const env = {
    ...process.env,
    ZMEM_ROOT: ROOT,
    ZMEM_STORE: path.join(tmp, "store.sqlite"),
    ZMEM_DATA: tmp,
    ZMEM_MODELS_DIR: path.join(tmp, "missing-models"),
    ZMEM_MODEL_AUTODOWNLOAD: "0",
    ZMEM_DEEPSEEK_STATE: statePath,
    ZMEM_NAMESPACE: "user:global",
    PYTHONIOENCODING: "utf-8",
  };
  // Seed one deterministic recallable row through the public CLI.
  const { execFileSync } = await import("node:child_process");
  const storePy = path.join(ROOT, "skills", "memory", "scripts", "store.py");
  try {
    execFileSync(process.env.PYTHON || "python", ["-X", "utf8", storePy,
      "add", "--namespace", "user:global", "--type", "fact",
      "--content", "canary seed row: deepseek native delivery probe",
      "--confidence", "0.9", "--signal", "user", "--json",
      "--source-ref", "session:ds-canary"], { env, cwd: ROOT, stdio: "ignore" });
  } catch (err) {
    console.error("canary: seed failed:", String(err).slice(0, 200));
    process.exit(1);
  }
  // Run the delivery + identity + capture scenarios through the frozen
  // driver (same surfaces the frozen checks exercise).
  const { execFile } = await import("node:child_process");
  // Per-scenario state files (each run REPLACES the state file, so a
  // shared path would leave only the LAST scenario's evidence).
  const run = (scenario) => new Promise((resolve) => {
    const sp = path.join(tmp, `state-${scenario}.json`);
    execFile("node", [path.join(ROOT, "tests", "fixtures", "deepseek",
      "scenario.mjs")],
      { env: { ...env, SCENARIO: scenario, ZMEM_DEEPSEEK_STATE: sp },
        cwd: ROOT, timeout: 120000 }, (err, stdout) => {
      let st = {};
      try { st = JSON.parse(fs.readFileSync(sp, "utf8")); } catch {}
      try {
        const line = (stdout || "").trim().split(/\r?\n/).pop();
        resolve({ ok: !err && line ? JSON.parse(line).ok === true : false,
          err: err ? String(err.message).slice(0, 160) : null, state: st });
      } catch {
        resolve({ ok: false, err: "unparseable driver output", state: st });
      }
    });
  });
  const first = await run("first_request_delivery");
  const identity = await run("identity_and_lifecycle");
  const corrections = await run("corrections_and_failures");
  const offline = await run("offline_snapshot");
  // The tools lane runs in LOCAL mode (no ZMEM_REMOTE_URL): zmem_add
  // enqueues and ops-flush --local COMMITS the receipt. In remote mode the
  // receipt stays pending by design (C12 exercises that separately).
  const localEnv = { ...env };
  delete localEnv.ZMEM_REMOTE_URL;
  const reconnect = await new Promise((resolve) => {
    const sp = path.join(tmp, "state-reconnect-local.json");
    execFile("node", [path.join(ROOT, "tests", "fixtures", "deepseek",
      "scenario.mjs")],
      { env: { ...localEnv, SCENARIO: "reflection_and_closeout",
        SCENARIO_EVIDENCE: "1",
        ZMEM_DEEPSEEK_STATE: sp }, cwd: ROOT, timeout: 120000 },
      (err, stdout) => {
        let st = {};
        try { st = JSON.parse(fs.readFileSync(sp, "utf8")); } catch {}
        resolve({ ok: !err, state: st });
      });
  });
  // Aggregate: acked/reserved from the DELIVERY leg; receipts from the
  // reconnect leg (the tools lane); compaction resets + identity from the
  // identity leg; capture from the corrections leg.
  const state = {
    acked: first.state.acked,
    reserved: first.state.reserved,
    receipts: reconnect.state.receipts
      || [reconnect.state.closeoutReceipt].filter(Boolean),
    compactionResets: identity.state.compactionResets || 0,
    captureQueue: corrections.state.captureQueue || [],
  };
  const gitSha = (await import("node:child_process")).execSync(
    "git rev-parse --short=12 HEAD", { cwd: ROOT, encoding: "utf8" })
    .trim();
  const report = {
    schema: "zmem-deepseek-canary-v1",
    generated_at: new Date().toISOString(),
    zmem_version: readManifestVersion(),
    runtime: { packages: await runtimeVersions() },
    claimed: [{ carrier: "headless", platform: process.platform,
      node: process.version }],
    lanes: {
      delivered: { status: first.ok ? "pass" : "fail",
        detail: "first accepted pre-step fenced delivery" },
      acked: { status: state.acked ? "pass" : "fail",
        detail: "reservation acknowledged after observed admission" },
      identity: { status: identity.ok ? "pass" : "fail",
        detail: "concurrent-agent + same-store principal isolation" },
      tools: { status:
        (state.receipts || []).some((r) => r.status === "committed")
          ? "pass" : "fail",
        detail: "explicit zmem_* tools committed receipt" },
      capture: { status:
        (state.captureQueue || []).length > 0 ? "pass" : "fail",
        detail: "redacted deduped capture entries" },
      compaction: { status:
        (state.compactionResets || 0) > 0 ? "pass" : "fail",
        detail: "synthetic compaction marker serialization "
          + "(real compactor package not in 0.2.1-rc.2 — disclosed)" },
      offline: { status: offline.ok ? "pass" : "fail",
        detail: "T-11 snapshot labelled offline delivery" },
    },
    corrections_leg: corrections,
    git_sha: process.env.ZMEM_CANARY_GIT_SHA || gitSha,
    environment: { host: "local-run", tmp: null },
  };
  fs.mkdirSync(path.dirname(path.resolve(out)), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(scrubHome(report), null, 1) + "\n");
  const failed = Object.values(report.lanes)
    .filter((l) => l.status !== "pass").length;
  console.log(`canary: wrote ${out} (${failed} failing lane(s))`);
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.error("canary:", err);
  process.exit(1);
});
