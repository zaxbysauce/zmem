#!/usr/bin/env node
// test_t08_broker_remote.js — Issue #287 (Workstream T PR 8) frozen check C1:
// remote passive memory through the launcher.
//
// Drives the REAL hooks/zmem-launch.js recall moment twice against the SAME
// fleet fixture payload (tests/fixtures/fleet/moments.json, moment=="recall")
// and IDENTICALLY seeded stores (tests/fixtures/fleet/seed.jsonl replayed
// through store.py):
//
//   LEG 1 (local)  — ZMEM_STORE=<local.sqlite> (seeded), no remote env: the
//                    current local path must deliver the seeded content.
//   LEG 2 (remote) — ZMEM_STORE=<empty.sqlite> (a THIRD store with NO seed —
//                    proves content came from the hub) plus
//                    ZMEM_REMOTE_URL=<in-process hub> (+ ZMEM_MCP_TOKEN).
//
// The hub is the real hermes-plugin/server/mcp_server.py, served on an
// ephemeral loopback port by tests/support/t08_hub.py against its own
// identically seeded store.
//
// Assert: the remote leg is nonempty, contains the seeded fixture content
// marker, and equals the local leg's additionalContext modulo volatile ids
// (uuid4 row ids minted at write time — the fixture README equality
// protocol, tests/fixtures/fleet/README.md:30-38). At BASE the launcher
// ignores ZMEM_REMOTE_URL, LEG 2 delivers nothing, and the decisive remote
// assertion throws the AssertionError the RED checkpoint anchors on.
//
// Run: node tests/test_t08_broker_remote.js   (exit 0 = pass)

"use strict";
require("./_store_guard.js"); // issue #244 isolation guard

const { spawn, spawnSync } = require("child_process");
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const LAUNCHER = path.join(REPO, "hooks", "zmem-launch.js");
const STORE_PY = path.join(REPO, "skills", "memory", "scripts", "store.py");
const HUB_PY = path.join(REPO, "tests", "support", "t08_hub.py");
const SEED_JSONL = path.join(REPO, "tests", "fixtures", "fleet", "seed.jsonl");
const MOMENTS_JSON = path.join(REPO, "tests", "fixtures", "fleet", "moments.json");
const HUB_TOKEN = "t08-hub-token";

const PYTHON = process.env.ZMEM_PYTHON || "python";

let passed = 0;
let failed = 0;
const failures = [];

function ok(name, cond, detail) {
    if (cond) {
        passed++;
        console.log("  PASS  " + name);
    } else {
        failed++;
        failures.push(name + (detail ? "  — " + detail : ""));
        console.log("  FAIL  " + name + (detail ? "  — " + detail : ""));
    }
}

function eq(name, actual, expected) {
    ok(name, actual === expected,
        `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

function sleepMs(ms) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

// --- helpers ---------------------------------------------------------------

// A clean env with all host/canonical vars stripped (the test_launcher.js
// envWith pattern), plus EVERY ambient ZMEM_* var — the issue #287 remote
// knobs (ZMEM_REMOTE_URL/ZMEM_MCP_*) and the store pins must never leak in
// from the operator's environment; the real ~/.zmem / ~/.zcode stores stay
// untouched.
function envWith(overrides) {
    const e = {};
    for (const [key, value] of Object.entries(process.env)) {
        if (key.startsWith("ZMEM_")) continue;
        e[key] = value;
    }
    for (const key of [
        "PLUGIN_ROOT", "PLUGIN_DATA", "CODEX_PROJECT_DIR",
        "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR",
        "ZCODE_PROJECT_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
        "CLAUDE_SESSION_ID", "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
        "ZCODE_SESSION_ID",
    ]) {
        delete e[key];
    }
    return Object.assign(e, overrides);
}

function seedStore(storePath) {
    // Replay the seed.jsonl argv recipe through store.py with ZMEM_STORE
    // pinned to this leg's isolated store (README.md:24-29 protocol), in
    // step order. Harness note (recorded deviation): the recipe's
    // signal=none default lands at confidence 0.2 — below the store's
    // CONFIDENCE_FLOOR (0.25) where NO recall lane can see the row — so the
    // replay pins --confidence 0.9 on every add (identically on both legs).
    const lines = fs.readFileSync(SEED_JSONL, "utf8").split("\n")
        .filter((l) => l.trim());
    const recipes = lines.map((l) => JSON.parse(l))
        .sort((a, b) => (a.step || 0) - (b.step || 0));
    for (const recipe of recipes) {
        const argv = recipe.argv.slice();
        if (argv[0] === "add" && !argv.includes("--confidence")) {
            argv.push("--confidence", "0.9");
        }
        const r = spawnSync(PYTHON, [STORE_PY, ...argv], {
            env: envWith({ ZMEM_STORE: storePath, PYTHONIOENCODING: "utf8" }),
            encoding: "utf8",
            timeout: 120000,
            cwd: REPO,
        });
        if (r.status !== 0) {
            throw new Error(`seed step ${recipe.step} failed (${r.status}): ` +
                `${(r.stderr || "").slice(0, 300)}`);
        }
    }
}

// The seeded-content marker, READ from the fixture (not retyped): step 1's
// --content value.
function seedMarker() {
    const first = JSON.parse(fs.readFileSync(SEED_JSONL, "utf8").split("\n")
        .filter((l) => l.trim())[0]);
    const idx = first.argv.indexOf("--content");
    assert.ok(idx > 0, "seed.jsonl step 1 carries --content");
    return first.argv[idx + 1].slice(0, 40);
}

// Mask volatile ids before the equality assertion: uuid4 row ids are minted
// at write time and rendered into the envelope header, so two independently
// seeded stores are NOT byte-identical — equality is asserted modulo the
// volatile id (the fixture README protocol).
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g;
const HEX32_RE = /\b[0-9a-f]{32}\b/g;
function maskVolatile(text) {
    return String(text).replace(UUID_RE, "<uuid>").replace(HEX32_RE, "<id>");
}

// --- fixture payload --------------------------------------------------------

const moments = JSON.parse(fs.readFileSync(MOMENTS_JSON, "utf8"));
const recallMoment = moments.moments.find(
    (m) => m.moment === "recall" &&
        m.payload.prompt ===
            "Fleet fixture recall prompt: how does the delivery ledger dedupe?");
assert.ok(recallMoment, "moments.json carries the recall fixture payload");

// --- scratch stores + synthetic project checkout ----------------------------

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-t08-remote-"));
const DATA_DIR = path.join(TMP, "data");
fs.mkdirSync(DATA_DIR, { recursive: true });
const LOCAL_STORE = path.join(TMP, "local.sqlite");
const HUB_STORE = path.join(TMP, "hub.sqlite");
const EMPTY_STORE = path.join(TMP, "empty.sqlite");

// The synthetic project checkout: a git repo whose origin normalizes to
// exactly "fixture-280", so the launcher's REAL namespace resolution (which
// overwrites ambient ZMEM_NAMESPACE for NEEDS_NAMESPACE hooks — see the
// check-author report) lands on project:fixture-280 for BOTH legs, the
// namespace the seed recipe stores under. (The spec's alternative — pinning
// ambient ZMEM_NAMESPACE — is dead at base: buildCanonicalEnv re-resolves
// it from cwd.)
const PROJ = path.join(TMP, "proj");
fs.mkdirSync(PROJ, { recursive: true });
const gitInit = spawnSync("git", ["init", "-q"], { cwd: PROJ, encoding: "utf8" });
const gitRemote = gitInit.status === 0
    ? spawnSync("git", ["remote", "add", "origin", "fixture-280"],
        { cwd: PROJ, encoding: "utf8" })
    : null;
ok("synthetic project checkout: git repo with origin fixture-280",
    gitRemote !== null && gitRemote.status === 0,
    `git init=${gitInit.status} remote=${gitRemote && gitRemote.status}`);

const PAYLOAD = JSON.stringify(Object.assign({}, recallMoment.payload, {
    cwd: PROJ,
}));

function runLauncherLeg(storePath, extraEnv) {
    const r = spawnSync("node", [LAUNCHER, "recall"], {
        input: PAYLOAD,
        env: envWith(Object.assign({
            ZMEM_STORE: storePath,
            ZMEM_DATA: DATA_DIR,
        }, extraEnv)),
        encoding: "utf8",
        timeout: 60000,
        cwd: REPO,
    });
    if (r.status !== 0) {
        return { failed: true, status: r.status, stderr: r.stderr || "" };
    }
    try {
        const obj = JSON.parse(r.stdout.trim());
        return {
            failed: false,
            context: obj && typeof obj.additionalContext === "string"
                ? obj.additionalContext : "",
            raw: r.stdout,
        };
    } catch (e) {
        return { failed: true, status: 0, stderr: "stdout not JSON: " +
            String(r.stdout).slice(0, 200) };
    }
}

// --- the run ----------------------------------------------------------------

let hub = null;
let hubStderrPath = null;
try {
    seedStore(LOCAL_STORE);
    seedStore(HUB_STORE);
    ok("fixture seed replayed into both isolated stores", true);

    const marker = seedMarker();
    ok("seed content marker read from seed.jsonl", marker.length > 0, marker);

    // LEG 1 (local): no remote env — today's local path against the seeded
    // store. This leg MUST deliver at base; it pins the harness, not the
    // feature (a broken local leg would make the A==B equality vacuous).
    const leg1 = runLauncherLeg(LOCAL_STORE, {});
    ok("local leg: launcher exited cleanly", !leg1.failed,
        leg1.failed ? `status=${leg1.status} ${String(leg1.stderr).slice(0, 200)}` : "");
    ok("local leg: additionalContext carries the seeded marker",
        !leg1.failed && leg1.context.includes(marker),
        leg1.failed ? "" : String(leg1.context).slice(0, 200));
    const A = leg1.failed ? "" : leg1.context;

    // The hub: real mcp_server on an ephemeral loopback port (seeded store,
    // token auth). stdout/stderr go to files we poll, so nothing can stall
    // on full pipes while this script blocks.
    const hubOutPath = path.join(TMP, "hub-stdout.log");
    hubStderrPath = path.join(TMP, "hub-stderr.log");
    const hubOutFd = fs.openSync(hubOutPath, "a");
    const hubErrFd = fs.openSync(hubStderrPath, "a");
    hub = spawn(PYTHON, [HUB_PY, "--store", HUB_STORE,
        "--namespace", "project:fixture-280"], {
        stdio: ["pipe", hubOutFd, hubErrFd],
        cwd: REPO,
    });
    fs.closeSync(hubOutFd);
    fs.closeSync(hubErrFd);
    let handshake = "";
    const hubDeadline = Date.now() + 30000;
    while (Date.now() < hubDeadline) {
        const text = fs.existsSync(hubOutPath)
            ? fs.readFileSync(hubOutPath, "utf8") : "";
        const nl = text.indexOf("\n");
        if (nl >= 0) {
            handshake = text.slice(0, nl).trim();
            break;
        }
        if (hub.exitCode !== null) {
            break;
        }
        sleepMs(200);
    }
    const hubDiedEarly = hub.exitCode !== null && !/^T08_HUB \d+$/.test(handshake);
    ok("hub helper reached its T08_HUB handshake",
        /^T08_HUB \d+$/.test(handshake),
        `handshake=${JSON.stringify(handshake)} hubExit=${hub.exitCode}`);
    if (hubDiedEarly) {
        const errText = fs.existsSync(hubStderrPath)
            ? fs.readFileSync(hubStderrPath, "utf8") : "";
        console.log("  hub died early; stderr:\n" + errText.slice(0, 2000));
    }
    assert.ok(/^T08_HUB \d+$/.test(handshake),
        "remote leg: the hub helper died before serving — see its stderr above");
    const hubUrl = `http://127.0.0.1:${handshake.split(" ")[1]}/mcp`;

    // LEG 2 (remote): an UNSEEDED third store + the hub URL. Anything in B
    // came over the wire.
    const leg2 = runLauncherLeg(EMPTY_STORE, {
        ZMEM_REMOTE_URL: hubUrl,
        ZMEM_MCP_TOKEN: HUB_TOKEN,
    });
    ok("remote leg: launcher exited cleanly", !leg2.failed,
        leg2.failed ? `status=${leg2.status} ${String(leg2.stderr).slice(0, 200)}` : "");
    const B = leg2.failed ? "" : leg2.context;

    // issue #287 AC1 — the decisive remote anchors. These THROW
    // AssertionError (not the soft ok() counter) so the base RED log anchors
    // on AssertionError + "remote": at base ZMEM_REMOTE_URL is dead, LEG 2
    // reads the empty local store, and B is "".
    assert.ok(typeof B === "string" && B.length > 0,
        "remote leg (ZMEM_REMOTE_URL=" + hubUrl + ") delivered an EMPTY " +
        "additionalContext — the launcher ignored the hub URL and read the " +
        "unseeded local store (issue #287: remote mode absent at base)");
    assert.ok(B.includes(marker),
        "remote leg: the hub-delivered envelope is missing the seeded " +
        "fixture content marker " + JSON.stringify(marker));
    assert.strictEqual(maskVolatile(A), maskVolatile(B),
        "remote leg envelope differs from the local-path envelope modulo " +
        "volatile ids (fixture README equality protocol)");

    ok("remote leg: envelope equals the local leg modulo volatile ids", true);
} finally {
    if (hub) {
        try { hub.kill(); } catch (e) { /* already gone */ }
    }
    try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* */ }
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
