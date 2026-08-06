#!/usr/bin/env node
// test_native_replace.js — Phase 6 tests for "replace native" (ZMEM_TIER0
// gating + the Claude-Code-only native-memory nudge).
//
// Drives the REAL zmem-launch.js + zmem-session-start.sh against a TEMP
// ZMEM_DATA store and a TEMP fake ~/.claude/settings.json (via HOME override —
// never touches the real ~/.claude/settings.json). Covers:
//   - ZMEM_TIER0=native: core.md + Tier2 inject, AGENTS.md is skipped
//   - ZMEM_TIER0=zmem (zcode): both core.md and AGENTS.md inject
//   - native-memory nudge fires once (claude host), then is suppressed by the
//     marker file
//   - nudge suppressed when settings.json has autoMemoryEnabled:false
//   - nudge never fires on the zcode host
//
// Run: node tests/test_native_replace.js   (exit 0 = all pass)

"use strict";

const { spawnSync, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const LAUNCHER = path.join(REPO, "hooks", "zmem-launch.js");
const STORE_PY = path.join(REPO, "skills", "memory", "scripts", "store.py");
const HOST_PY = path.join(REPO, "skills", "memory", "scripts", "host.py");

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
        failures.push(name + (detail ? "  — " + detail : ""));
        console.log("  FAIL  " + name + (detail ? "  — " + detail : ""));
    }
}

function envWith(overrides) {
    const e = { ...process.env };
    for (const k of [
        "ZMEM_HOST", "ZMEM_ROOT", "ZMEM_DATA", "ZMEM_STORE", "ZMEM_PROJECT", "ZMEM_SESSION",
        "ZMEM_TRANSCRIPT", "ZMEM_AGENT_TYPE", "ZMEM_NAMESPACE", "ZMEM_SKILLS_DIRS",
        "ZMEM_TIER0", "ZMEM_CTX_BUDGET",
        "PLUGIN_ROOT", "PLUGIN_DATA", "CODEX_PROJECT_DIR",
        "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR",
        "ZCODE_PROJECT_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
        "CLAUDE_SESSION_ID", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "HOME", "USERPROFILE",
    ]) {
        delete e[k];
    }
    return Object.assign(e, overrides);
}

function runLauncher(hook, payload, env) {
    return spawnSync("node", [LAUNCHER, hook], {
        input: payload,
        env,
        encoding: "utf8",
        timeout: 30000,
    });
}

function resolveNs(projectDir) {
    const code =
        "import sys; sys.path.insert(0, sys.argv[1]); import host; " +
        "print(host.resolve_namespace(sys.argv[2]))";
    return execFileSync(
        PYTHON,
        ["-c", code, path.dirname(HOST_PY), projectDir],
        { encoding: "utf8" }
    ).trim();
}

// --- temp workspace --------------------------------------------------------
const TMP_ROOT = path.join(REPO, ".tmp-tests");
fs.mkdirSync(TMP_ROOT, { recursive: true });
const TMP = fs.mkdtempSync(path.join(TMP_ROOT, "zmem-p6-"));
const PROJ = path.join(TMP, "proj");
const FAKE_HOME = path.join(TMP, "home");
fs.mkdirSync(PROJ, { recursive: true });
fs.mkdirSync(path.join(FAKE_HOME, ".claude"), { recursive: true });
fs.writeFileSync(path.join(PROJ, "AGENTS.md"), "PROJECT-LEVEL-CONVENTIONS-MARKER");

const NS = resolveNs(PROJ);

// store.py's connect() hardens a BRAND NEW data dir's perms via icacls
// (P1, store.py:169-174, "only right after we created the dir/file"). On
// this box that icacls call has been observed to leave the directory
// unreadable to the very same account moments later (pre-existing P1
// behavior, independent of this phase) if a second connect() lands while
// it is mid-flight. Sidestep it here rather than fight it: build one
// "template" store.sqlite (letting that hardening happen once, in a
// throwaway dir we never read from again), then seed every real test data
// dir with a COPY of it plus core.md before the dir is ever handed to the
// real hook — so store.py's connect() always sees dir_existed AND
// file_existed already True and never re-triggers the hardening path.
const TEMPLATE_DIR = fs.mkdtempSync(path.join(TMP, "template-store-"));
execFileSync(PYTHON, [STORE_PY, "stats"], {
    env: envWith({ ZMEM_DATA: TEMPLATE_DIR }),
    encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
});
const TEMPLATE_STORE = path.join(TEMPLATE_DIR, "store.sqlite");

function seedDataDir(dataDir, coreMdContent) {
    fs.mkdirSync(dataDir, { recursive: true });
    fs.copyFileSync(TEMPLATE_STORE, path.join(dataDir, "store.sqlite"));
    fs.writeFileSync(path.join(dataDir, "core.md"), coreMdContent);
    return dataDir;
}

const DATA = seedDataDir(path.join(TMP, "data"), "# Core Memory (seeded for tests 1-2)\n");

const SESSION_PAYLOAD = JSON.stringify({
    session_id: "sess-p6",
    transcript_path: "C:\\tmp\\transcript.jsonl",
    cwd: PROJ,
    hook_event_name: "SessionStart",
    source: "startup",
});

// A fake HOME with no settings.json at all (native memory implicitly still on).
function freshHome() {
    const h = fs.mkdtempSync(path.join(TMP, "home-"));
    fs.mkdirSync(path.join(h, ".claude"), { recursive: true });
    return h;
}

function extractAdditionalContext(stdout) {
    let obj = null;
    try { obj = JSON.parse(stdout.trim()); } catch (e) { /* */ }
    if (!obj) return null;
    if (obj.hookSpecificOutput) return obj.hookSpecificOutput.additionalContext || "";
    return obj.additionalContext || "";
}

console.log("\n[1] ZMEM_TIER0=native (claude): core.md + Tier2 inject, AGENTS.md skipped");
{
    const home = freshHome();
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: DATA, ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ, ZMEM_NAMESPACE: NS,
        ZMEM_HOST: "claude", ZMEM_TIER0: "native", ZMEM_CTX_BUDGET: "9000",
        HOME: home, USERPROFILE: home,
    }));
    ok("exit 0", r.status === 0, "status=" + r.status + " stderr=" + (r.stderr || "").slice(0, 300));
    const ctx = extractAdditionalContext(r.stdout);
    ok("stdout parses to an envelope", ctx !== null, r.stdout.slice(0, 200));
    ok("hookSpecificOutput present (claude envelope)", /hookSpecificOutput/.test(r.stdout));
    ok("core.md (Tier 0) present", /Tier 0 . core\.md/.test(ctx || "") || /Core Memory/.test(ctx || ""));
    ok("AGENTS.md is ABSENT under native tier0", !/PROJECT-LEVEL-CONVENTIONS-MARKER/.test(ctx || ""), (ctx || "").slice(0, 300));
    ok("store.py path (Tier 2 plumbing) present", /store\.py/.test(ctx || ""));
}

console.log("\n[2] ZMEM_TIER0=zmem (zcode): core.md AND AGENTS.md both inject");
{
    const home = freshHome();
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: DATA, ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ, ZMEM_NAMESPACE: NS,
        ZMEM_HOST: "zcode", ZMEM_TIER0: "zmem", ZMEM_CTX_BUDGET: "25000",
        HOME: home, USERPROFILE: home,
    }));
    ok("exit 0", r.status === 0);
    const ctx = extractAdditionalContext(r.stdout);
    ok("bare additionalContext (zcode envelope)", !/hookSpecificOutput/.test(r.stdout));
    ok("AGENTS.md IS present under zmem tier0", /PROJECT-LEVEL-CONVENTIONS-MARKER/.test(ctx || ""), (ctx || "").slice(0, 300));
}

console.log("\n[3] Native-memory nudge: fires once on claude host, then suppressed");
{
    const home = freshHome();
    const overrides = {
        ZMEM_DATA: path.join(TMP, "nudge-data-1"), ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ,
        ZMEM_NAMESPACE: NS, ZMEM_HOST: "claude", ZMEM_TIER0: "native",
        ZMEM_CTX_BUDGET: "9000", HOME: home, USERPROFILE: home,
    };
    seedDataDir(overrides.ZMEM_DATA, "# Core Memory (pre-seeded)\n");

    const r1 = runLauncher("session-start", SESSION_PAYLOAD, envWith(overrides));
    const ctx1 = extractAdditionalContext(r1.stdout) || "";
    ok("first run: nudge present", /ZMem notice/.test(ctx1), ctx1.slice(0, 200));
    ok("marker file created", fs.existsSync(path.join(overrides.ZMEM_DATA, ".native-nudge-shown")));

    const r2 = runLauncher("session-start", SESSION_PAYLOAD, envWith(overrides));
    const ctx2 = extractAdditionalContext(r2.stdout) || "";
    ok("second run: nudge suppressed", !/ZMem notice/.test(ctx2), ctx2.slice(0, 200));
    ok("second run: core.md still present (nudge suppression is isolated)",
        /Core Memory \(pre-seeded\)/.test(ctx2), ctx2.slice(0, 200));
}

console.log("\n[4] Nudge suppressed when settings.json has autoMemoryEnabled:false");
{
    const home = freshHome();
    fs.writeFileSync(path.join(home, ".claude", "settings.json"),
        JSON.stringify({ autoMemoryEnabled: false }));
    const dataDir = path.join(TMP, "nudge-data-2");
    seedDataDir(dataDir, "# Core Memory (seeded)\n");
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: dataDir, ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ, ZMEM_NAMESPACE: NS,
        ZMEM_HOST: "claude", ZMEM_TIER0: "native", ZMEM_CTX_BUDGET: "9000",
        HOME: home, USERPROFILE: home,
    }));
    const ctx = extractAdditionalContext(r.stdout) || "";
    ok("nudge suppressed by autoMemoryEnabled:false", !/ZMem notice/.test(ctx), ctx.slice(0, 200));
    ok("no marker file written (nudge never shown, nothing to guard)",
        !fs.existsSync(path.join(dataDir, ".native-nudge-shown")));
}

console.log("\n[5] Nudge suppressed via settings.local.json (not just settings.json)");
{
    const home = freshHome();
    fs.writeFileSync(path.join(home, ".claude", "settings.local.json"),
        JSON.stringify({ autoMemoryEnabled: false }));
    const dataDir = path.join(TMP, "nudge-data-3");
    seedDataDir(dataDir, "# Core Memory (seeded)\n");
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: dataDir, ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ, ZMEM_NAMESPACE: NS,
        ZMEM_HOST: "claude", ZMEM_TIER0: "native", ZMEM_CTX_BUDGET: "9000",
        HOME: home, USERPROFILE: home,
    }));
    const ctx = extractAdditionalContext(r.stdout) || "";
    ok("nudge suppressed by settings.local.json", !/ZMem notice/.test(ctx), ctx.slice(0, 200));
}

console.log("\n[6] Malformed settings.json fails open (nudge still fires, no crash)");
{
    const home = freshHome();
    fs.writeFileSync(path.join(home, ".claude", "settings.json"), "{ not valid json ,,,");
    const dataDir = path.join(TMP, "nudge-data-4");
    seedDataDir(dataDir, "# Core Memory (seeded)\n");
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: dataDir, ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ, ZMEM_NAMESPACE: NS,
        ZMEM_HOST: "claude", ZMEM_TIER0: "native", ZMEM_CTX_BUDGET: "9000",
        HOME: home, USERPROFILE: home,
    }));
    ok("exit 0 despite malformed settings.json", r.status === 0);
    const ctx = extractAdditionalContext(r.stdout) || "";
    ok("nudge still fires (malformed file treated as unset)", /ZMem notice/.test(ctx), ctx.slice(0, 200));
}

console.log("\n[7] Nudge never fires on the zcode host, regardless of settings.json");
{
    const home = freshHome(); // no settings.json at all
    const dataDir = path.join(TMP, "nudge-data-5");
    seedDataDir(dataDir, "# Core Memory (seeded)\n");
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: dataDir, ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ, ZMEM_NAMESPACE: NS,
        ZMEM_HOST: "zcode", ZMEM_TIER0: "zmem", ZMEM_CTX_BUDGET: "25000",
        HOME: home, USERPROFILE: home,
    }));
    const ctx = extractAdditionalContext(r.stdout) || "";
    ok("zcode host: no nudge ever", !/ZMem notice/.test(ctx), ctx.slice(0, 200));
    ok("zcode host: no marker file written",
        !fs.existsSync(path.join(dataDir, ".native-nudge-shown")));
}

console.log("\n[8] CLAUDE_CODE_DISABLE_AUTO_MEMORY env also suppresses the nudge");
{
    const home = freshHome();
    const dataDir = path.join(TMP, "nudge-data-6");
    seedDataDir(dataDir, "# Core Memory (seeded)\n");
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: dataDir, ZMEM_ROOT: REPO, ZMEM_PROJECT: PROJ, ZMEM_NAMESPACE: NS,
        ZMEM_HOST: "claude", ZMEM_TIER0: "native", ZMEM_CTX_BUDGET: "9000",
        HOME: home, USERPROFILE: home, CLAUDE_CODE_DISABLE_AUTO_MEMORY: "1",
    }));
    const ctx = extractAdditionalContext(r.stdout) || "";
    ok("nudge suppressed by CLAUDE_CODE_DISABLE_AUTO_MEMORY", !/ZMem notice/.test(ctx), ctx.slice(0, 200));
}

// --- cleanup + report --------------------------------------------------------
try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* */ }

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
