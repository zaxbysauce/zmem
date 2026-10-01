#!/usr/bin/env node
// Issue #242: causal proof that SessionStart's maintenance worker is detached.
// The fixture delays only the exact maintenance wrapper and observes its marker
// before it permits the owned process to complete; no wall-clock threshold is
// an acceptance condition.

"use strict";

const { spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const [repo, projectDir, expectedNamespace, mode] = process.argv.slice(2);
const HELPER_DEADLINE_MS = 32000;
const CLEANUP_RESERVE_MS = 12000;
const ORGANIZER_MAX_WAIT_MS = HELPER_DEADLINE_MS + CLEANUP_RESERVE_MS + 1000;
const START_WAIT_MS = 12000;
const CLOSE_WAIT_MS = 10000;
const MUTANT_OBSERVE_MS = 1200;
const DELAYED_CADENCE =
    "import time, subprocess, sys; time.sleep(15); sys.exit(subprocess.call([sys.executable] + sys.argv[1:]))";

if (!repo || !projectDir || !expectedNamespace || !["real", "mutant"].includes(mode)) {
    process.stderr.write("usage: maintenance-causal.js REPO PROJECT NAMESPACE real|mutant\n");
    process.exit(2);
}

const startedAt = Date.now();
const workDeadline = startedAt + HELPER_DEADLINE_MS;
let work = "";
let releasePath = "";
let startedPath = "";
let finishedPath = "";
let pidPath = "";
let launcher = null;
let launcherClosed = false;
let closePromise = null;
let realPython = "";

const summary = {
    mode,
    helper_deadline_ms: HELPER_DEADLINE_MS,
    cleanup_reserve_ms: CLEANUP_RESERVE_MS,
    copied_launcher_exact: false,
    copied_session_start_exact: false,
    mutant_changed_one_ampersand: false,
    python_shim_probe: "",
    project_dir: path.resolve(projectDir),
    requested_namespace: expectedNamespace,
    seed_namespace: "",
    seed_namespace_matches_project: false,
    store_path: "",
    data_dir: "",
    organizer_started: false,
    launcher_closed_before_release: false,
    session_start_valid: false,
    seeded_context_present: false,
    delivery_before_release: false,
    delivery_after_release: false,
    outer_timeout: false,
    organizer_finished: false,
    organizer_exited: false,
    launcher_exit_code: null,
    launcher_close_signal: null,
    cleanup_worker_survived: false,
    organizer_timed_out: false,
    real_python_path: "",
    schema_before: "",
    schema_after: "",
    mutant_falsified: false,
    stdout: "",
    stderr: "",
    failure: null,
};

function remainingMs(limit) {
    return Math.max(0, Math.min(limit, workDeadline - Date.now()));
}

function boundedTimeout(limit, label) {
    const timeout = remainingMs(limit);
    if (timeout <= 0) throw new Error("helper deadline exhausted before " + label);
    return timeout;
}

function sha256(file) {
    return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function copyTree(source, target) {
    const stat = fs.lstatSync(source);
    if (stat.isSymbolicLink()) throw new Error("fixture source contains a symlink: " + source);
    if (stat.isDirectory()) {
        fs.mkdirSync(target, { recursive: true });
        for (const entry of fs.readdirSync(source)) {
            if (entry === "__pycache__" || entry.endsWith(".pyc")) continue;
            copyTree(path.join(source, entry), path.join(target, entry));
        }
        return;
    }
    fs.copyFileSync(source, target);
}

function waitForFile(file, limit) {
    const deadline = Date.now() + Math.max(0, limit);
    return new Promise((resolve) => {
        const poll = () => {
            if (fs.existsSync(file)) return resolve(true);
            if (Date.now() >= deadline || remainingMs(limit) <= 0) return resolve(false);
            setTimeout(poll, Math.min(25, deadline - Date.now(), remainingMs(limit)));
        };
        poll();
    });
}

function waitFor(promise, limit) {
    return new Promise((resolve) => {
        const timer = setTimeout(() => resolve(null), Math.max(0, limit));
        promise.then((value) => {
            clearTimeout(timer);
            resolve(value);
        });
    });
}

function waitForOwnedExit(pid, limit) {
    const deadline = Date.now() + Math.max(0, limit);
    return new Promise((resolve) => {
        const poll = () => {
            try {
                process.kill(pid, 0);
            } catch (error) {
                if (error && error.code === "ESRCH") return resolve(true);
            }
            if (Date.now() >= deadline) return resolve(false);
            setTimeout(poll, Math.min(25, deadline - Date.now()));
        };
        poll();
    });
}

function ownedWorkerPid() {
    if (!fs.existsSync(pidPath)) return null;
    const pid = Number(fs.readFileSync(pidPath, "utf8").trim());
    return Number.isInteger(pid) && pid > 0 ? pid : null;
}

function findPython() {
    // Resolve sys.executable before the shim enters PATH.  The optional value
    // is a harness override; portable source never prefers a fixed path.
    const candidates = [];
    if (process.env.ZMEM_TEST_REAL_PYTHON) candidates.push(process.env.ZMEM_TEST_REAL_PYTHON);
    candidates.push("python3", "python");
    for (const candidate of candidates) {
        const result = spawnSync(candidate, ["-c", "import sys; print(sys.executable)"], {
            encoding: "utf8", stdio: ["ignore", "pipe", "ignore"],
            timeout: boundedTimeout(3000, "Python interpreter discovery"),
        });
        const resolved = String(result.stdout || "").trim();
        if (result.status === 0 && path.isAbsolute(resolved)) return resolved;
    }
    throw new Error("could not resolve an absolute Python interpreter before installing the fixture shim");
}

function isolatedEnv(overrides) {
    const env = { ...process.env };
    const selectors = new Set([
        "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR",
        "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY", "PLUGIN_DATA", "PLUGIN_ROOT",
        "ZCODE_PLUGIN_DATA", "ZCODE_PLUGIN_ROOT", "ZCODE_PROJECT_DIR",
    ]);
    for (const key of Object.keys(env)) {
        if (key.startsWith("ZMEM_") || selectors.has(key)) delete env[key];
    }
    return Object.assign(env, overrides);
}

function runPython(args, env, label) {
    const result = spawnSync(realPython, args, {
        cwd: work, env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
        timeout: boundedTimeout(5000, label),
    });
    if (result.status !== 0) {
        throw new Error(label + " failed: " + String(result.stderr || result.stdout || "").slice(-800));
    }
    return String(result.stdout || "").trim();
}

function resolveFixtureNamespace(fixtureRoot, env) {
    const scripts = path.join(fixtureRoot, "skills", "memory", "scripts");
    return runPython([
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); import host; print(host.resolve_namespace(sys.argv[2]))",
        scripts,
        path.resolve(projectDir),
    ], env, "fixture namespace resolution");
}

function schemaVersion(storePath, env) {
    return runPython([
        "-c",
        "import sqlite3, sys; c=sqlite3.connect(sys.argv[1]); r=c.execute(\"SELECT value FROM meta WHERE key='schema_version'\").fetchone(); print(r[0] if r else '')",
        storePath,
    ], env, "fixture schema read");
}

function writePythonShim(dir) {
    // Node on Windows resolves an extensionless command literally, rather
    // than through PATHEXT, so a .cmd wrapper cannot serve both Node's
    // namespace probe and Git Bash.  Link the real interpreter under both
    // command names and use Python's import-time site shim to intercept only
    // the exact `-c` maintenance wrapper.  Every ordinary argument executes
    // the discovered absolute interpreter unchanged.
    const siteDir = path.join(dir, "site");
    fs.mkdirSync(siteDir, { recursive: true });
    const siteShim = `import os, sys, time, subprocess
_target = ${JSON.stringify(DELAYED_CADENCE)}
_argv = getattr(sys, "orig_argv", [])
try:
    _at = _argv.index("-c")
except ValueError:
    _at = -1
if _at >= 0 and _at + 1 < len(_argv) and _argv[_at + 1] == _target:
    with open(os.environ["ZMEM_TEST_ORGANIZER_PID"], "w", encoding="utf-8") as _f:
        _f.write(str(os.getpid()))
    with open(os.environ["ZMEM_TEST_ORGANIZER_STARTED"], "w", encoding="utf-8") as _f:
        _f.write("organizer-started")
    _deadline = time.monotonic() + (float(os.environ["ZMEM_TEST_ORGANIZER_MAX_WAIT_MS"]) / 1000.0)
    while (not os.path.exists(os.environ["ZMEM_TEST_ORGANIZER_RELEASE"])
           and time.monotonic() < _deadline):
        time.sleep(0.025)
    if not os.path.exists(os.environ["ZMEM_TEST_ORGANIZER_RELEASE"]):
        with open(os.environ["ZMEM_TEST_ORGANIZER_TIMEOUT"], "w", encoding="utf-8") as _f:
            _f.write("organizer-timeout")
        raise SystemExit(0)
    with open(os.environ["ZMEM_TEST_ORGANIZER_FINISHED"], "w", encoding="utf-8") as _f:
        _f.write("organizer-finished")
    time.sleep = lambda _seconds: None
    subprocess.call = lambda *_args, **_kwargs: 0
`;
    fs.writeFileSync(path.join(siteDir, "sitecustomize.py"), siteShim, "utf8");
    const names = process.platform === "win32" ? ["python.exe", "python3.exe"] : ["python", "python3"];
    for (const name of names) {
        const target = path.join(dir, name);
        try {
            fs.linkSync(realPython, target);
        } catch (error) {
            fs.symlinkSync(realPython, target);
        }
    }
    return siteDir;
}

function readDelivery() {
    try {
        const envelope = JSON.parse(summary.stdout.trim());
        const context = envelope && envelope.hookSpecificOutput && envelope.hookSpecificOutput.additionalContext;
        summary.session_start_valid = !!(envelope && envelope.hookSpecificOutput &&
            envelope.hookSpecificOutput.hookEventName === "SessionStart");
        summary.seeded_context_present = typeof context === "string" &&
            context.includes("MAINTENANCE_CAUSAL_SEED");
    } catch (error) {
        summary.failure = summary.failure || "launcher stdout was not valid JSON: " + error.message;
    }
}

async function main() {
    realPython = findPython();
    summary.real_python_path = realPython;
    if (!path.isAbsolute(realPython)) {
        throw new Error("resolved interpreter is not absolute: " + realPython);
    }
    work = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-maintenance-" + mode + "-"));
    const fixtureRoot = path.join(work, "plugin");
    const shimDir = path.join(work, "python-shim");
    const dataDir = path.join(work, "data");
    const storePath = path.join(dataDir, "store.sqlite");
    startedPath = path.join(work, "organizer-started");
    releasePath = path.join(work, "organizer-release");
    finishedPath = path.join(work, "organizer-finished");
    pidPath = path.join(work, "organizer-pid");
    const timeoutPath = path.join(work, "organizer-timeout");
    summary.data_dir = dataDir;
    summary.store_path = storePath;

    fs.mkdirSync(fixtureRoot, { recursive: true });
    fs.mkdirSync(shimDir, { recursive: true });
    fs.mkdirSync(dataDir, { recursive: true });
    copyTree(path.join(repo, "hooks"), path.join(fixtureRoot, "hooks"));
    copyTree(path.join(repo, "skills", "memory", "scripts"), path.join(fixtureRoot, "skills", "memory", "scripts"));

    const sourceLauncher = path.join(repo, "hooks", "zmem-launch.js");
    const copiedLauncher = path.join(fixtureRoot, "hooks", "zmem-launch.js");
    const sourceSessionStart = path.join(repo, "hooks", "zmem-session-start.sh");
    const copiedSessionStart = path.join(fixtureRoot, "hooks", "zmem-session-start.sh");
    summary.copied_launcher_exact = sha256(sourceLauncher) === sha256(copiedLauncher);
    summary.copied_session_start_exact = sha256(sourceSessionStart) === sha256(copiedSessionStart);
    if (!summary.copied_launcher_exact || !summary.copied_session_start_exact) {
        throw new Error("fixture copy was not byte-identical before the controlled mutation");
    }
    if (mode === "mutant") {
        const before = fs.readFileSync(copiedSessionStart, "utf8");
        const needle = ">>\"$BG_SINK\" 2>&1 &";
        if (before.split(needle).length - 1 !== 1) throw new Error("maintenance background anchor was not unique");
        const after = before.replace(needle, ">>\"$BG_SINK\" 2>&1");
        fs.writeFileSync(copiedSessionStart, after, "utf8");
        const countAmpersands = (text) => (text.match(/&/g) || []).length;
        summary.mutant_changed_one_ampersand = countAmpersands(before) - countAmpersands(after) === 1 &&
            Buffer.byteLength(before) - Buffer.byteLength(after) === 2;
    }

    const baseEnv = isolatedEnv({
        ZMEM_HOME: fixtureRoot,
        ZMEM_STORE: storePath,
        ZMEM_DATA: dataDir,
        ZMEM_MODELS_DIR: path.join(work, "missing-models"),
        ZMEM_MODEL_AUTODOWNLOAD: "0",
        ZMEM_CAPTURE: "0",
        ZMEM_BG_LOG: "1",
        ZMEM_LAUNCHER_WATCHDOG_MS: "25000",
        CLAUDE_PLUGIN_ROOT: fixtureRoot,
        CLAUDE_PROJECT_DIR: path.resolve(projectDir),
        ZMEM_TEST_NODE: process.execPath,
        ZMEM_TEST_REAL_PYTHON: realPython,
        ZMEM_TEST_ORGANIZER_PID: pidPath,
        ZMEM_TEST_ORGANIZER_STARTED: startedPath,
        ZMEM_TEST_ORGANIZER_RELEASE: releasePath,
        ZMEM_TEST_ORGANIZER_FINISHED: finishedPath,
        ZMEM_TEST_ORGANIZER_MAX_WAIT_MS: String(ORGANIZER_MAX_WAIT_MS),
        ZMEM_TEST_ORGANIZER_TIMEOUT: timeoutPath,
    });
    summary.seed_namespace = resolveFixtureNamespace(fixtureRoot, baseEnv);
    summary.seed_namespace_matches_project = summary.seed_namespace === expectedNamespace;
    if (!summary.seed_namespace_matches_project) throw new Error("seed namespace differs from the explicit project namespace");
    const fixtureStore = path.join(fixtureRoot, "skills", "memory", "scripts", "store.py");
    runPython([fixtureStore, "add", "--namespace", summary.seed_namespace, "--type", "lesson",
        "--content", "MAINTENANCE_CAUSAL_SEED must arrive before maintenance release.",
        "--confidence", "0.9", "--signal", "test"], baseEnv, "fixture seed");
    summary.schema_before = schemaVersion(storePath, baseEnv);
    if (!summary.schema_before || !fs.existsSync(storePath)) throw new Error("isolated fixture store was not initialized");
    const siteDir = writePythonShim(shimDir);
    const interpreterDir = path.dirname(realPython);
    const env = {
        ...baseEnv,
        PATH: shimDir + path.delimiter + interpreterDir + path.delimiter + (baseEnv.PATH || ""),
        PYTHONPATH: siteDir + path.delimiter + (baseEnv.PYTHONPATH || ""),
        PYTHONDONTWRITEBYTECODE: "1",
    };
    if (process.platform === "win32") env.PYTHONHOME = interpreterDir;
    const shimProbe = spawnSync("python", ["-c", "print(41)"], {
        env, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
        timeout: boundedTimeout(3000, "python shim probe"),
    });
    summary.python_shim_probe = JSON.stringify({
        status: shimProbe.status,
        stdout: String(shimProbe.stdout || "").trim(),
        stderr: String(shimProbe.stderr || "").trim(),
        error: shimProbe.error && shimProbe.error.message,
    });
    if (shimProbe.status !== 0 || String(shimProbe.stdout || "").trim() !== "41") {
        throw new Error("python shim did not forward ordinary arguments: " + summary.python_shim_probe);
    }
    const payload = JSON.stringify({
        session_id: "issue242-maintenance-" + mode + "-" + process.pid,
        transcript_path: path.join(work, "transcript.jsonl"),
        cwd: path.resolve(projectDir), hook_event_name: "SessionStart", source: "startup",
    });
    const output = [];
    const errors = [];
    launcher = spawn(process.execPath, [copiedLauncher, "session-start"], {
        cwd: fixtureRoot, env, stdio: ["pipe", "pipe", "pipe"],
    });
    launcher.stdout.on("data", (chunk) => output.push(Buffer.from(chunk)));
    launcher.stderr.on("data", (chunk) => errors.push(Buffer.from(chunk)));
    closePromise = new Promise((resolve) => {
        launcher.once("error", (error) => resolve({ error }));
        launcher.once("close", (code, signal) => {
            launcherClosed = true;
            summary.launcher_exit_code = code;
            summary.launcher_close_signal = signal;
            resolve({ code, signal });
        });
    });
    launcher.stdin.end(payload);

    summary.organizer_started = await waitForFile(startedPath, Math.min(START_WAIT_MS, remainingMs(START_WAIT_MS)));
    const closeWait = mode === "mutant" ? MUTANT_OBSERVE_MS : CLOSE_WAIT_MS;
    const closeResult = await waitFor(closePromise, Math.min(closeWait, remainingMs(closeWait)));
    summary.launcher_closed_before_release = !!closeResult && closeResult.code === 0 && !fs.existsSync(releasePath);
    summary.stdout = Buffer.concat(output).toString("utf8");
    summary.stderr = Buffer.concat(errors).toString("utf8");
    if (summary.launcher_closed_before_release) readDelivery();
    summary.outer_timeout = fs.existsSync(path.join(dataDir, "zmem-decisions.log")) &&
        fs.readFileSync(path.join(dataDir, "zmem-decisions.log"), "utf8").includes("outer_timeout=1");
    summary.delivery_before_release = summary.launcher_closed_before_release && summary.session_start_valid &&
        summary.seeded_context_present && !summary.outer_timeout;

    fs.writeFileSync(releasePath, "organizer-release\n", "utf8");
    summary.organizer_finished = await waitForFile(finishedPath, Math.min(5000, remainingMs(5000)));
    if (!launcherClosed) await waitFor(closePromise, Math.min(8000, remainingMs(8000)));
    summary.stdout = Buffer.concat(output).toString("utf8");
    summary.stderr = Buffer.concat(errors).toString("utf8");
    readDelivery();
    summary.delivery_after_release = launcherClosed && summary.launcher_exit_code === 0 && summary.session_start_valid &&
        summary.seeded_context_present && !summary.outer_timeout;
    const workerPid = ownedWorkerPid();
    summary.organizer_exited = !!workerPid && await waitForOwnedExit(workerPid, Math.min(2000, remainingMs(2000)));
    summary.organizer_timed_out = fs.existsSync(timeoutPath);
    summary.schema_after = schemaVersion(storePath, baseEnv);

    if (mode === "real") {
        if (!summary.organizer_started || !summary.delivery_before_release || !summary.organizer_finished ||
            summary.organizer_timed_out ||
            !summary.organizer_exited || summary.launcher_exit_code !== 0 ||
            summary.schema_before !== summary.schema_after) {
            throw new Error("real fixture did not prove detached maintenance before valid delivery");
        }
    } else {
        summary.mutant_falsified = summary.organizer_started && !summary.organizer_timed_out &&
            summary.mutant_changed_one_ampersand &&
            !summary.launcher_closed_before_release && !summary.delivery_before_release &&
            summary.organizer_finished && summary.organizer_exited && summary.delivery_after_release;
        if (!summary.mutant_falsified) throw new Error("foreground mutant did not block valid delivery until the owned release");
    }
}

async function cleanup() {
    const cleanupDeadline = Date.now() + CLEANUP_RESERVE_MS;
    const cleanupRemaining = () => Math.max(0, cleanupDeadline - Date.now());
    try { if (launcher && !launcherClosed) launcher.kill(); } catch { /* owned launcher */ }
    try { if (releasePath) fs.writeFileSync(releasePath, "organizer-release\n", "utf8"); } catch { /* owned marker */ }
    if (startedPath && !summary.organizer_started) {
        summary.organizer_started = await waitForFile(startedPath, cleanupRemaining());
    }
    if (summary.organizer_started && !summary.organizer_finished) {
        summary.organizer_finished = await waitForFile(finishedPath, cleanupRemaining());
    }
    const workerPid = ownedWorkerPid();
    if (workerPid && !summary.organizer_exited) {
        summary.organizer_exited = await waitForOwnedExit(workerPid, cleanupRemaining());
    }
    if (workerPid && !summary.organizer_exited) {
        try { process.kill(workerPid); } catch { /* owned shim only */ }
        summary.organizer_exited = await waitForOwnedExit(workerPid, Math.min(1000, cleanupRemaining()));
        if (!summary.organizer_exited) {
            summary.cleanup_worker_survived = true;
            summary.failure = summary.failure || "owned maintenance worker survived cleanup";
        }
    }
    if (work && launcherClosed && (!summary.organizer_started || summary.organizer_exited)) {
        try { fs.rmSync(work, { recursive: true, force: true }); } catch { /* temp cleanup is best effort */ }
    }
}

(async () => {
    try { await main(); }
    catch (error) { summary.failure = String(error && error.stack || error); }
    await cleanup();
    process.stdout.write(JSON.stringify(summary) + "\n");
    process.exit(summary.failure ? 1 : 0);
})().catch((error) => {
    process.stdout.write(JSON.stringify({ ...summary, failure: String(error) }) + "\n");
    process.exit(1);
});
