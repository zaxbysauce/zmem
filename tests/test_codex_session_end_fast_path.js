#!/usr/bin/env node
// Focused issue #189 coverage for the Codex SessionEnd fast path.

"use strict";

const assert = require("assert");
const crypto = require("crypto");
const { EventEmitter } = require("events");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const REPO = path.resolve(__dirname, "..");
const LAUNCHER = path.join(REPO, "hooks", "zmem-launch.js");
const launch = require(LAUNCHER);
let passed = 0;

function check(name, predicate) {
    try {
        assert.ok(predicate());
        passed++;
        console.log("  PASS  " + name);
    } catch (error) {
        console.log("  FAIL  " + name + " — " + error.message);
        throw error;
    }
}

function runLauncher(pluginRoot, payload, env) {
    return runLauncherInput(pluginRoot, JSON.stringify(payload), env);
}

function runLauncherInput(pluginRoot, input, env) {
    return spawnSync(process.execPath, [path.join(pluginRoot, "hooks", "zmem-launch.js"), "session-end"], {
        input,
        env,
        cwd: pluginRoot,
        encoding: "utf8",
        timeout: 15000,
    });
}

async function testHelpers() {
    check("payload session_id wins over inherited env", () => {
        const id = launch.codexSessionEndId({ session_id: "  payload-id  ", sessionId: "other" });
        return id === "payload-id" && launch.codexSessionEndId({ sessionId: "  camel-id " }) === "camel-id"
            && launch.codexSessionEndId({ session_id: "  ", sessionId: 17 }) === ""
            && launch.codexSessionEndId({}) === "";
    });

    check("environment preserves explicit store and removes inherited session", () => {
        const env = launch.codexSessionEndEnv({ ZMEM_DATA: "data", ZMEM_STORE: "store", ZMEM_SESSION: "stale" });
        return env.ZMEM_DATA === "data" && env.ZMEM_STORE === "store"
            && !Object.prototype.hasOwnProperty.call(env, "ZMEM_SESSION");
    });

    let timerCallback;
    let timerMs;
    let timerCleared = false;
    const order = [];
    const child = new EventEmitter();
    child.kill = () => order.push("kill");
    const closePromise = launch.runCodexSessionEndFastPath(
        { session_id: "sid" },
        {
            env: { ZMEM_DATA: "data", ZMEM_STORE: "store", ZMEM_SESSION: "stale", ZMEM_PYTHON: "python-override" },
            pluginRoot: "plugin-root",
            platform: "win32",
            now: () => 0,
            setTimeoutFn: (fn, ms) => { order.push("timer"); timerCallback = fn; timerMs = ms; return 7; },
            clearTimeoutFn: () => { timerCleared = true; },
            spawnFn: (python, argv, options) => {
                order.push("spawn");
                assert.strictEqual(python, "python-override");
                assert.deepStrictEqual(argv, [
                    "plugin-root\\skills\\memory\\scripts\\store.py",
                    "ledger-clear", "--session-id", "sid",
                ]);
                assert.strictEqual(options.stdio, "ignore");
                assert.strictEqual(options.env.ZMEM_DATA, "data");
                assert.strictEqual(options.env.ZMEM_STORE, "store");
                assert.strictEqual(options.env.ZMEM_SESSION, undefined);
                return child;
            },
        },
    );
    check("fast path arms deadline before direct child", () => order.join(",") === "timer,spawn" && timerMs === 1900);
    child.emit("close", 0);
    const closeResult = await closePromise;
    check("fast path awaits successful child and clears timer", () =>
        closeResult.ok === true && timerCleared && order.indexOf("kill") === -1);

    let hungTimer;
    let killed = false;
    const hungChild = new EventEmitter();
    hungChild.kill = () => { killed = true; };
    const hungPromise = launch.runCodexSessionEndFastPath(
        { session_id: "hung" },
        {
            env: {}, now: () => 1800,
            setTimeoutFn: (fn) => { hungTimer = fn; return 1; },
            clearTimeoutFn: () => {},
            spawnFn: () => hungChild,
        },
    );
    hungTimer();
    const hungResult = await hungPromise;
    check("hung child fails open and is killed at remaining deadline", () =>
        hungResult.reason === "timeout" && killed);

    let spawned = false;
    const noBudget = await launch.runCodexSessionEndFastPath(
        { session_id: "late" },
        { now: () => launch.CODEX_SESSION_END_TARGET_MS + 1, spawnFn: () => { spawned = true; } },
    );
    check("elapsed startup with no remaining budget skips spawn", () => noBudget.reason === "no-budget" && !spawned);

    const errorChild = new EventEmitter();
    const errorResultPromise = launch.runCodexSessionEndFastPath(
        { session_id: "error" },
        { now: () => 0, spawnFn: () => errorChild, setTimeoutFn: () => 1, clearTimeoutFn: () => {} },
    );
    errorChild.emit("error", new Error("synthetic"));
    const errorResult = await errorResultPromise;
    check("asynchronous spawn error settles fail open", () => errorResult.reason === "spawn-error");

    const throwResult = await launch.runCodexSessionEndFastPath(
        { session_id: "throw" },
        { now: () => 0, spawnFn: () => { throw new Error("synthetic"); }, setTimeoutFn: () => 1, clearTimeoutFn: () => {} },
    );
    check("synchronous spawn error settles fail open", () => throwResult.reason === "spawn-throw");

    const nonzeroChild = new EventEmitter();
    const nonzeroPromise = launch.runCodexSessionEndFastPath(
        { session_id: "nonzero" },
        { now: () => 0, spawnFn: () => nonzeroChild, setTimeoutFn: () => 1, clearTimeoutFn: () => {} },
    );
    nonzeroChild.emit("close", 9);
    const nonzeroResult = await nonzeroPromise;
    check("nonzero child exit settles fail open", () => nonzeroResult.completed && nonzeroResult.ok === false && nonzeroResult.code === 9);

    const posixChild = new EventEmitter();
    let selectedPython = "";
    const posixPromise = launch.runCodexSessionEndFastPath(
        { session_id: "posix" },
        { env: {}, platform: "linux", now: () => 0, spawnFn: (python) => { selectedPython = python; return posixChild; }, setTimeoutFn: () => 1, clearTimeoutFn: () => {} },
    );
    posixChild.emit("close", 0);
    await posixPromise;
    check("POSIX selects python3 without where probing", () => selectedPython === "python3");

    const rootChild = new EventEmitter();
    let selectedRoot = "";
    const rootPromise = launch.runCodexSessionEndFastPath(
        { session_id: "root" },
        {
            env: { PLUGIN_ROOT: "preferred-root", ZMEM_ROOT: "stale-root" },
            now: () => 0,
            spawnFn: (_python, _argv, options) => { selectedRoot = options.cwd; return rootChild; },
            setTimeoutFn: () => 1,
            clearTimeoutFn: () => {},
        },
    );
    rootChild.emit("close", 0);
    await rootPromise;
    check("PLUGIN_ROOT takes precedence over stale ZMEM_ROOT", () => selectedRoot === "preferred-root");
}

function makeTree() {
    const tree = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-189-"));
    const pluginRoot = path.join(tree, "plugin");
    const dataDir = path.join(tree, "data");
    const marker = path.join(tree, "marker.json");
    fs.mkdirSync(path.join(pluginRoot, "hooks"), { recursive: true });
    fs.mkdirSync(path.join(pluginRoot, "skills", "memory", "scripts"), { recursive: true });
    fs.copyFileSync(LAUNCHER, path.join(pluginRoot, "hooks", "zmem-launch.js"));
    return { tree, pluginRoot, dataDir, marker };
}

function baseEnv(pluginRoot, dataDir, marker) {
    const env = { ...process.env };
    for (const key of ["ZMEM_HOST", "ZMEM_ROOT", "ZMEM_DATA", "ZMEM_STORE", "ZMEM_SESSION",
        "PLUGIN_ROOT", "PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR",
        "ZCODE_PLUGIN_ROOT", "ZCODE_PROJECT_DIR", "ZMEM_PYTHON", "ZMEM_BASH_PATH"]) {
        delete env[key];
    }
    Object.assign(env, {
        PLUGIN_ROOT: pluginRoot,
        ZMEM_HOST: "codex",
        ZMEM_DATA: dataDir,
        ZMEM_STORE: path.join(dataDir, "store.sqlite"),
        ZMEM_SESSION: "stale-inherited-session",
        ZMEM_MARKER: marker,
        ZMEM_PYTHON: process.execPath,
    });
    return env;
}

function sidecarPath(dataDir, sessionId, suffix) {
    const stem = crypto.createHash("sha256").update(sessionId, "utf8").digest("hex").slice(0, 32);
    return path.join(dataDir, "ops", stem + suffix);
}

async function testRealLauncher() {
    const { tree, pluginRoot, dataDir, marker } = makeTree();
    try {
        fs.mkdirSync(dataDir, { recursive: true });
        // This is an argv-recording ZMEM_PYTHON stub. Its .py name keeps the
        // exact production store.py argv while Node executes the test stub.
        fs.writeFileSync(path.join(pluginRoot, "skills", "memory", "scripts", "store.py"), [
            "const fs = require('fs');",
            "fs.appendFileSync(process.env.ZMEM_MARKER, JSON.stringify({ argv: process.argv.slice(2), data: process.env.ZMEM_DATA, store: process.env.ZMEM_STORE, session: process.env.ZMEM_SESSION }) + '\\n');",
            "process.stdout.write('child stdout noise\\n');",
            "process.stderr.write('child stderr noise\\n');",
        ].join("\n"));
        const result = runLauncher(pluginRoot, { session_id: "  live-session  " }, baseEnv(pluginRoot, dataDir, marker));
        check("real Codex launcher emits exact empty JSON LF", () => result.status === 0 && result.stdout === "{}\n" && result.stderr === "");
        const records = fs.readFileSync(marker, "utf8").trim().split(/\r?\n/).map((line) => JSON.parse(line));
        const recorded = records[0];
        check("real Codex launcher exercises one direct ledger-clear child", () =>
            records.length === 1 && recorded.argv.length === 3 && recorded.argv[0] === "ledger-clear"
            && recorded.argv[1] === "--session-id" && recorded.argv[2] === "live-session"
            && recorded.data === dataDir && recorded.store === path.join(dataDir, "store.sqlite")
            && recorded.session === undefined);

        fs.rmSync(marker, { force: true });
        const noId = runLauncher(pluginRoot, {}, baseEnv(pluginRoot, dataDir, marker));
        check("missing payload id does not use stale inherited session", () =>
            noId.status === 0 && noId.stdout === "{}\n" && !fs.existsSync(marker));

        const whitespace = runLauncher(pluginRoot, { session_id: "  ", sessionId: 19 }, baseEnv(pluginRoot, dataDir, marker));
        const malformed = runLauncherInput(pluginRoot, "not-json", baseEnv(pluginRoot, dataDir, marker));
        const overlarge = runLauncherInput(pluginRoot,
            JSON.stringify({ session_id: "x".repeat(256 * 1024) }), baseEnv(pluginRoot, dataDir, marker));
        check("malformed, overlarge, whitespace, and non-string ids fail open without spawning", () =>
            whitespace.status === 0 && whitespace.stdout === "{}\n"
            && malformed.status === 0 && malformed.stdout === "{}\n"
            && overlarge.status === 0 && overlarge.stdout === "{}\n"
            && !fs.existsSync(marker));

        const defaultDataEnv = baseEnv(pluginRoot, dataDir, marker);
        delete defaultDataEnv.ZMEM_DATA;
        const defaultData = runLauncher(pluginRoot, { session_id: "default-data" }, defaultDataEnv);
        const defaultRecords = fs.readFileSync(marker, "utf8").trim().split(/\r?\n/).map((line) => JSON.parse(line));
        check("missing ZMEM_DATA defaults to ~/.zmem while explicit store is preserved", () =>
            defaultData.status === 0 && defaultData.stdout === "{}\n"
            && defaultRecords.length === 1 && defaultRecords[0].data === path.join(os.homedir(), ".zmem")
            && defaultRecords[0].store === path.join(dataDir, "store.sqlite"));

        fs.rmSync(marker, { force: true });
        const bogusEnv = baseEnv(pluginRoot, dataDir, marker);
        bogusEnv.ZMEM_PYTHON = path.join(tree, "missing-python");
        const bogus = runLauncher(pluginRoot, { session_id: "bogus" }, bogusEnv);
        check("bogus interpreter fails open with exact output", () =>
            bogus.status === 0 && bogus.stdout === "{}\n" && !fs.existsSync(marker));
    } finally {
        fs.rmSync(tree, { recursive: true, force: true });
    }
}

function testDivergentStoreAndDataRoots() {
    const tree = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-189-roots-"));
    const storeDir = path.join(tree, "store-root");
    const defaultDataDir = path.join(tree, "default-data");
    const sessionId = "divergent-root-session";
    try {
        for (const dataDir of [storeDir, defaultDataDir]) {
            fs.mkdirSync(path.join(dataDir, "ops"), { recursive: true });
            for (const suffix of [".ledger", ".pending", ".compact", ".tasktext"]) {
                fs.writeFileSync(sidecarPath(dataDir, sessionId, suffix), suffix + "\n");
            }
        }
        const env = { ...process.env };
        for (const key of ["ZMEM_HOST", "ZMEM_ROOT", "ZMEM_DATA", "ZMEM_STORE", "ZMEM_PYTHON",
            "PLUGIN_ROOT", "PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR",
            "ZCODE_PLUGIN_ROOT", "ZCODE_PROJECT_DIR", "ZMEM_SESSION"]) {
            delete env[key];
        }
        Object.assign(env, {
            PLUGIN_ROOT: REPO,
            ZMEM_HOST: "codex",
            ZMEM_DATA: defaultDataDir,
            ZMEM_STORE: path.join(storeDir, "store.sqlite"),
            ZMEM_MODELS_DIR: path.join(tree, "models"),
            ZMEM_MODEL_AUTODOWNLOAD: "0",
        });
        const result = spawnSync(process.execPath, [LAUNCHER, "session-end"], {
            input: JSON.stringify({ session_id: sessionId }),
            env,
            cwd: REPO,
            encoding: "utf8",
            timeout: 15000,
        });
        const storeGone = [".ledger", ".pending"].every((suffix) =>
            !fs.existsSync(sidecarPath(storeDir, sessionId, suffix)));
        const storePreserved = [".compact", ".tasktext"].every((suffix) =>
            fs.existsSync(sidecarPath(storeDir, sessionId, suffix)));
        const defaultPreserved = [".ledger", ".pending", ".compact", ".tasktext"].every((suffix) =>
            fs.existsSync(sidecarPath(defaultDataDir, sessionId, suffix)));
        check("divergent ZMEM_STORE root clears only ledger and pending beside store", () =>
            result.status === 0 && result.stdout === "{}\n" && result.stderr === ""
            && storeGone && storePreserved && defaultPreserved);
    } finally {
        fs.rmSync(tree, { recursive: true, force: true });
    }
}

function testNonCodexNegative() {
    const { tree, pluginRoot, dataDir, marker } = makeTree();
    try {
        fs.writeFileSync(path.join(pluginRoot, "hooks", "zmem-session-end.sh"),
            "#!/usr/bin/env bash\nprintf '%s\\n' body-ran > \"$ZMEM_MARKER\"\nprintf '%s\\n' body-output\n");
        const env = baseEnv(pluginRoot, dataDir, marker);
        env.ZMEM_HOST = "claude";
        delete env.ZMEM_PYTHON;
        env.CLAUDE_PLUGIN_ROOT = pluginRoot;
        delete env.PLUGIN_ROOT;
        const result = runLauncher(pluginRoot, { session_id: "claude-session" }, env);
        check("non-Codex SessionEnd remains on existing body route", () =>
            result.status === 0 && result.stdout === "body-output\n"
            && fs.readFileSync(marker, "utf8") === "body-ran\n");
    } finally {
        fs.rmSync(tree, { recursive: true, force: true });
    }
}

(async () => {
    console.log("\n[1] Codex SessionEnd fast path helpers");
    await testHelpers();
    console.log("\n[2] Codex SessionEnd fast path launcher integration");
    await testRealLauncher();
    console.log("\n[3] Non-Codex SessionEnd negative");
    testNonCodexNegative();
    console.log("\n[4] Divergent store/data roots");
    testDivergentStoreAndDataRoots();
    console.log(`\n${passed} passed, 0 failed`);
})().catch(() => process.exitCode = 1);
