#!/usr/bin/env node
// Native DeepSeek canary runner (issues #374 AC7 / #375 AC6).
// Usage: node canary.mjs --out <path>
//
// Boots the real pinned dsh runtime + the plugin against an ISOLATED store,
// exercises delivery/ack/tools/capture/offline/compaction/policy legs, and
// writes the version-bound canary report (schema zmem-deepseek-canary-v1;
// the claim-gate test validates it). No model provider, no network to a
// model.
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
  // Policy lane (round 3): exercise the knowledge routing policy helper
  // in BOTH modes against the canary's isolated store dir — no vault, no
  // network, no model, nothing outside the canary temp dir. (a) The
  // default-off leg runs the helper with NO --config so it resolves the
  // config beside the ISOLATED store (no knowledge object exists there
  // yet) and must render the bounded EMPTY policy block: enabled false,
  // zero target ids, no text, capture off. (b) The enabled leg writes a
  // scratch config.json beside the SAME store with knowledge.enabled=true
  // plus one markdown target rooted inside this temp dir (the exact
  // load_knowledge_config grammar; skills/memory/scripts/knowledge/
  // config.py is authoritative) and the render must NAME the target id
  // while staying under the helper's documented 200-word cap. A checkout
  // that ships no policy helper (the companion stack renders no routing
  // policy) passes on that stack's default-off contract — no helper
  // means no policy render anywhere — with the enabled leg disclosed as
  // not applicable; the lane FAILS whenever the helper exists and any
  // render is missing, the enabled state is wrong, the target id is
  // absent/present in the wrong leg, or output is unbounded.
  const POLICY_PY = path.join(ROOT, "skills", "memory", "scripts",
    "knowledge", "policy.py");
  const policyEnv = () => {
    const child = {
      ZMEM_STORE: path.join(tmp, "store.sqlite"),
      ZMEM_DATA: tmp,
      PYTHONIOENCODING: "utf-8",
    };
    for (const key of ["HOME", "USERPROFILE", "USERNAME", "SystemRoot",
      "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP"]) {
      if (process.env[key] !== undefined) child[key] = process.env[key];
    }
    return child;
  };
  const runPolicy = (args) => new Promise((resolve) => {
    execFile(process.env.PYTHON || "python",
      ["-X", "utf8", POLICY_PY, ...args, "--json"],
      { env: policyEnv(), cwd: ROOT, timeout: 30000 },
      (err, stdout) => {
        let json = null;
        try {
          json = JSON.parse((stdout || "").trim().split(/\r?\n/).pop());
        } catch {}
        resolve({
          ok: !err,
          bytes: Buffer.byteLength(stdout || "", "utf8"),
          err: err ? String(err.message).slice(0, 160) : null,
          json,
        });
      });
  });
  const policy = await (async () => {
    const lane = { status: "fail", detail: "", legs: {} };
    if (!fs.existsSync(POLICY_PY)) {
      lane.status = "pass";
      lane.detail = "policy helper absent from this checkout: the "
        + "default-off contract holds (no helper => no policy render "
        + "anywhere); enabled leg not applicable";
      lane.legs = { helper: "absent", disabled: "default-off-by-contract",
        enabled: "not-applicable" };
      return lane;
    }
    // (a) default-off: resolver-based config lookup beside the isolated
    // store must yield the empty, bounded, capture-free disabled block.
    const off = await runPolicy(["--no-capture"]);
    lane.legs.disabled = {
      ok: off.ok, err: off.err, reason: off.json && off.json.reason,
      enabled: off.json ? off.json.enabled : null,
      targets: off.json ? (off.json.targets || []).length : null,
      word_count: off.json ? off.json.word_count : null,
      capture: off.json ? off.json.capture : null,
      bytes: off.bytes };
    const offOk = off.ok && off.json && off.json.enabled === false
      && (off.json.targets || []).length === 0
      && off.json.text === "" && off.json.word_count === 0
      && off.json.capture === false && off.bytes <= 2048;
    // (b) knowledge.enabled=true: markdown target rooted INSIDE the
    // canary temp dir; the roster must name the target id, word-capped.
    const vault = path.join(tmp, "policy-vault");
    fs.mkdirSync(vault, { recursive: true });
    const configPath = path.join(tmp, "config.json");
    fs.writeFileSync(configPath, JSON.stringify({
      knowledge: { enabled: true, targets: {
        kb: { kind: "markdown", root: vault } } } }));
    const on = await runPolicy(["--config", configPath]);
    lane.legs.enabled = {
      ok: on.ok, err: on.err, reason: on.json && on.json.reason,
      enabled: on.json ? on.json.enabled : null,
      target_id: on.json
        ? ((on.json.targets || []).includes("kb")
          && typeof on.json.text === "string"
          && on.json.text.includes("kb")) : null,
      word_count: on.json ? on.json.word_count : null,
      bytes: on.bytes };
    const onOk = on.ok && on.json && on.json.enabled === true
      && (on.json.targets || []).includes("kb")
      && typeof on.json.text === "string" && on.json.text.includes("kb")
      && typeof on.json.word_count === "number"
      && on.json.word_count > 0 && on.json.word_count < 200
      && on.bytes <= 8192;
    lane.status = offOk && onOk ? "pass" : "fail";
    lane.detail = "knowledge policy render: default-off empty block + "
      + "knowledge.enabled=true markdown-target roster (word-capped)";
    return lane;
  })();
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
      policy: { status: policy.status, detail: policy.detail,
        legs: policy.legs },
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
