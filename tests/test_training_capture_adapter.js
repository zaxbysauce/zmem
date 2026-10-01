#!/usr/bin/env node
// Focused launcher adapter coverage for automatic partial capture.

"use strict";

const assert = require("assert");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");

const REPO = path.resolve(__dirname, "..");
const launch = require(path.join(REPO, "hooks", "zmem-launch.js"));

let passed = 0;

function test(name, fn) {
    try {
        fn();
        passed++;
        console.log("  PASS  " + name);
    } catch (error) {
        console.error("  FAIL  " + name + " — " + error.message);
        throw error;
    }
}

function fakeExec(calls, result = { capture_id: "capture-1", state: "partial" }) {
    return (_python, args, options) => {
        calls.push({ args, options, input: JSON.parse(options.input) });
        return JSON.stringify(result);
    };
}

for (const host of ["claude", "codex", "zcode"]) {
    test(`${host} starts a partial and records the exact delivery snapshot`, () => {
        const calls = [];
        const env = {
            ...process.env,
            ZMEM_ROOT: REPO,
            ZMEM_SESSION: "session-hook-1",
            ZMEM_NAMESPACE: "project:hook-adapter",
        };
        const meta = {
            session_id: "session-hook-1",
            namespace: "project:hook-adapter",
            prompt: "Bearer should never be persisted by the hook",
            host_task_id: "host-task-1",
            turn_id: "turn-1",
        };
        const execFn = fakeExec(calls);

        const started = launch.runTrainingCapture(host, "recall", meta, env,
            "start", {}, execFn);
        assert.strictEqual(started.capture_id, "capture-1");
        assert.strictEqual(calls[0].args[1], "--action");
        assert.strictEqual(calls[0].args[2], "start");
        assert.strictEqual(calls[0].input.host, host);
        assert.strictEqual(calls[0].input.namespace, "project:hook-adapter");
        assert.strictEqual(calls[0].input.prompt, meta.prompt);
        assert.strictEqual(calls[0].input.host_task_id, "host-task-1");
        assert.strictEqual(calls[0].input.task_id, "host-task-1");
        assert.strictEqual(calls[0].input.capture_key, "turn-1");

        const delivered = launch.snapshotTrainingDelivery(host, "recall", meta,
            env, { rendered: "exact rendered context", effective_ops: ["run tests"] }, execFn);
        assert.strictEqual(delivered.capture_id, "capture-1");
        assert.strictEqual(calls[1].args[2], "snapshot");
        assert.strictEqual(calls[1].input.rendered, "exact rendered context");
        assert.deepStrictEqual(calls[1].input.effective_ops, ["run tests"]);
    });
}

test("task and session metadata are not promoted to a turn sidecar key", () => {
    const calls = [];
    const env = {
        ...process.env,
        ZMEM_ROOT: REPO,
        ZMEM_SESSION: "session-shared",
        ZMEM_NAMESPACE: "project:hook-adapter",
    };
    const meta = {
        session_id: "session-shared",
        namespace: "project:hook-adapter",
        task_id: "task-shared",
        host_task_id: "task-shared",
    };
    launch.runTrainingCapture("claude", "recall", meta, env, "start", {}, fakeExec(calls));
    assert.strictEqual(Object.prototype.hasOwnProperty.call(calls[0].input, "capture_key"), false);
});

test("launcher start capture is synchronous and uses the 5 second bound", () => {
    const calls = [];
    let returned = false;
    const env = {
        ...process.env,
        ZMEM_ROOT: REPO,
        ZMEM_SESSION: "session-bounded",
        ZMEM_NAMESPACE: "project:hook-adapter",
    };
    const execFn = (_python, args, options) => {
        assert.strictEqual(returned, false);
        calls.push({ args, options, input: JSON.parse(options.input) });
        return JSON.stringify({ capture_id: "capture-bounded", state: "partial" });
    };
    const result = launch.runTrainingCapture("claude", "recall", {
        session_id: "session-bounded",
        namespace: "project:hook-adapter",
        turn_id: "turn-bounded",
    }, env, "start", {}, execFn);
    returned = true;
    assert.strictEqual(result.capture_id, "capture-bounded");
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].args[2], "start");
    // Interpreter discovery is included in the same capture deadline.  Direct
    // module calls get the private five-second cap.
    assert(calls[0].options.timeout > 0);
    assert(calls[0].options.timeout <= 5000);
});

test("capture allocation binds to the carried deadline and debits the probe", () => {
    let now = 1000;
    const probes = [];
    const calls = [];
    const env = {
        ...process.env,
        ZMEM_ROOT: REPO,
        ZMEM_SESSION: "session-allocation",
        ZMEM_NAMESPACE: "project:allocation",
    };
    delete env.ZMEM_PYTHON;
    const clock = () => now;
    const unavailable = process.platform === "win32" ? "python" : "python3";
    const probe = (candidate, _args, options) => {
        probes.push({ candidate, timeout: options.timeout });
        if (candidate === unavailable) {
            now += 125;
            throw new Error("PATH candidate unavailable");
        }
    };
    const execFn = (_python, args, options) => {
        calls.push({ args, options });
        return JSON.stringify({ capture_id: "capture-allocation", state: "partial" });
    };
    const result = launch.runTrainingCapture("claude", "recall", {
        session_id: "session-allocation",
        namespace: "project:allocation",
        turn_id: "turn-allocation",
    }, env, "start", {}, execFn, undefined, {
        deadline: 5000,
        now: clock,
        probe,
    });
    assert.strictEqual(result.capture_id, "capture-allocation");
    assert.strictEqual(probes.length, 2);
    assert.strictEqual(probes[0].timeout, 250);
    assert(probes[1].timeout > 0 && probes[1].timeout < probes[0].timeout,
        `probe timeouts did not share the allocation: ${JSON.stringify(probes)}`);
    assert.strictEqual(calls.length, 1);
    // deadline-now-output-reserve = 5000-1000-500 = 3500, less the 125ms
    // PATH probe debit. A fresh five-second child timeout would violate this.
    assert.strictEqual(calls[0].options.timeout, 3375);
});

test("an exhausted carried allocation skips capture and null uses the direct cap", () => {
    const env = { ...process.env, ZMEM_ROOT: REPO, ZMEM_SESSION: "session-exhausted" };
    let probed = false;
    const exhausted = launch.runTrainingCapture("claude", "recall", {
        session_id: "session-exhausted",
    }, env, "start", {}, () => {
        throw new Error("capture must be skipped");
    }, undefined, {
        deadline: 5000,
        now: () => 4500,
        probe: () => { probed = true; },
    });
    assert.deepStrictEqual(exhausted, {});
    assert.strictEqual(probed, false);

    const calls = [];
    const direct = launch.runTrainingCapture("claude", "recall", {
        session_id: "session-direct",
    }, { ...env, ZMEM_SESSION: "session-direct", ZMEM_PYTHON: "python" },
    "start", {}, (_python, _args, options) => {
        calls.push(options);
        return JSON.stringify({ capture_id: "capture-direct", state: "partial" });
    });
    assert.strictEqual(direct.capture_id, "capture-direct");
    assert(calls[0].timeout > 0 && calls[0].timeout <= 5000);

    const nullDeadlineCalls = [];
    const nullDeadline = launch.runTrainingCapture("claude", "recall", {
        session_id: "session-null-deadline",
    }, { ...env, ZMEM_SESSION: "session-null-deadline", ZMEM_PYTHON: "python" },
    "start", {}, (_python, _args, options) => {
        nullDeadlineCalls.push(options);
        return JSON.stringify({ capture_id: "capture-null", state: "partial" });
    }, undefined, { deadline: null, now: () => 4500 });
    assert.strictEqual(nullDeadline.capture_id, "capture-null");
    assert(nullDeadlineCalls[0].timeout > 0 && nullDeadlineCalls[0].timeout <= 5000);

    const smallProbes = [];
    const smallCalls = [];
    const small = launch.runTrainingCapture("claude", "recall", {
        session_id: "session-small",
    }, { ...env, ZMEM_SESSION: "session-small" }, "start", {},
    (_python, _args, options) => {
        smallCalls.push(options);
        return JSON.stringify({ capture_id: "capture-small", state: "partial" });
    }, undefined, {
        deadline: 5000,
        now: () => 4300,
        probe: (_candidate, _args, options) => smallProbes.push(options.timeout),
    });
    assert.strictEqual(small.capture_id, "capture-small");
    assert.deepStrictEqual(smallProbes, [200]);
    assert.strictEqual(smallCalls[0].timeout, 200);

    let exhaustedProbeCalls = 0;
    let exhaustedChildCalls = 0;
    let probeTick = 4300;
    const probeExhausted = launch.runTrainingCapture("claude", "recall", {
        session_id: "session-probe-exhausted",
    }, { ...env, ZMEM_SESSION: "session-probe-exhausted" }, "start", {},
    () => {
        exhaustedChildCalls += 1;
        return JSON.stringify({ capture_id: "unexpected", state: "partial" });
    }, undefined, {
        deadline: 5000,
        now: () => probeTick,
        probe: () => {
            exhaustedProbeCalls += 1;
            probeTick += 201;
            throw new Error("probe exhausted the allocation");
        },
    });
    assert.deepStrictEqual(probeExhausted, {});
    assert.strictEqual(exhaustedProbeCalls, 1);
    assert.strictEqual(exhaustedChildCalls, 0);
});

test("the production main path clamps start capture under a small watchdog", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-capture-watchdog-"));
    const hooks = path.join(root, "hooks");
    const resolverBin = path.join(root, "resolver-bin");
    const captureStarted = path.join(root, "capture-started.marker");
    fs.mkdirSync(path.join(hooks, "lib"), { recursive: true });
    fs.mkdirSync(resolverBin, { recursive: true });
    if (process.platform === "win32") {
        fs.writeFileSync(path.join(resolverBin, "python.cmd"),
            "@echo off\r\necho {\"ns\":\"project:watchdog\",\"remote\":true}\r\n",
            "utf8");
    } else {
        const resolver = path.join(resolverBin, "python");
        fs.writeFileSync(resolver,
            "#!/bin/sh\nprintf '{\"ns\":\"project:watchdog\",\"remote\":true}\\n'\n",
            "utf8");
        fs.chmodSync(resolver, 0o755);
    }
    fs.writeFileSync(path.join(hooks, "zmem-recall.sh"),
        "#!/usr/bin/env bash\n" +
        "printf '<<<ZMEM_JSON>>>{\"additionalContext\":\"watchdog context\"}<<<END>>>\\n'\n",
        "utf8");
    fs.writeFileSync(path.join(hooks, "lib", "zmem-training-capture.py"),
        "if (process.argv[3] !== 'start') process.exit(0);\n" +
        "require('fs').writeFileSync(" + JSON.stringify(captureStarted) + ", 'start');\n" +
        "setTimeout(() => process.stdout.write('{}\\n'), 3000);\n",
        "utf8");
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
        !key.startsWith("ZMEM_")
        && !["PLUGIN_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"].includes(key)
    ));
    Object.assign(env, {
        PLUGIN_ROOT: root,
        ZMEM_ROOT: root,
        ZMEM_HOST: "claude",
        ZMEM_CAPTURE: "1",
        ZMEM_PYTHON: process.execPath,
        PATH: resolverBin + path.delimiter + env.PATH,
        ZMEM_BASH_PATH: launch.resolveShell(),
        ZMEM_LAUNCHER_WATCHDOG_MS: "1500",
        ZMEM_SESSION: "watchdog-session",
        ZMEM_NAMESPACE: "project:watchdog",
        ZMEM_STORE: path.join(root, "store.sqlite"),
        ZMEM_DATA: path.join(root, "data"),
        ZMEM_DECISION_LOG: path.join(root, "decisions.jsonl"),
    });
    try {
        const started = Date.now();
        const result = spawnSync(process.execPath,
            [path.join(REPO, "hooks", "zmem-launch.js"), "recall"], {
                input: JSON.stringify({
                    session_id: "watchdog-session",
                    namespace: "project:watchdog",
                    cwd: root,
                    prompt: "watchdog prompt",
                }),
                env,
                encoding: "utf8",
                timeout: 5000,
            });
        const elapsed = Date.now() - started;
        assert.strictEqual(result.status, 0, result.stderr);
        assert(elapsed < 2500,
            `small watchdog allowed a fresh capture allocation: ${elapsed}ms`);
        assert.strictEqual(fs.readFileSync(captureStarted, "utf8"), "start",
            "the production start helper must execute; skipping capture is not a clamp proof");
        const envelope = JSON.parse(result.stdout.trim());
        assert.strictEqual(envelope.hookSpecificOutput.additionalContext, "watchdog context",
            `expected the hook body result, got ${result.stdout}`);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

test("python resolver honors explicit override and platform candidate order", () => {
    const posixSeen = [];
    const posix = launch.resolvePython({}, "linux", (candidate) => {
        posixSeen.push(candidate);
        if (candidate !== "python") throw new Error("missing");
    });
    assert.strictEqual(posix, "python");
    assert.deepStrictEqual(posixSeen, ["python3", "python"]);

    const windowsSeen = [];
    const windows = launch.resolvePython({}, "win32", (candidate) => {
        windowsSeen.push(candidate);
        if (candidate !== "python3") throw new Error("missing");
    });
    assert.strictEqual(windows, "python3");
    assert.deepStrictEqual(windowsSeen, ["python", "python3"]);

    let probed = false;
    assert.strictEqual(
        launch.resolvePython({ ZMEM_PYTHON: "custom-python" }, "linux", () => {
            probed = true;
        }),
        "custom-python",
    );
    assert.strictEqual(probed, false);
    assert.strictEqual(
        launch.resolvePython({}, "linux", () => { throw new Error("missing"); }),
        "python3",
    );
    assert.strictEqual(
        launch.resolvePython({}, "win32", () => { throw new Error("missing"); }),
        "python",
    );

    const budgeted = [];
    let first = true;
    launch.resolvePython({}, "linux", (_candidate, _args, options) => {
        budgeted.push(options.timeout);
        if (first) {
            first = false;
            Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
            throw new Error("first candidate unavailable");
        }
    }, 100);
    assert.strictEqual(budgeted.length, 2);
    assert(budgeted[0] <= 100 && budgeted[1] > 0 && budgeted[1] < budgeted[0],
        `probe timeouts did not share one deadline: ${budgeted}`);
});

test("recall start enforces the carried deadline when a helper runs long", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-capture-timeout-"));
    const hooks = path.join(root, "hooks", "lib");
    fs.mkdirSync(hooks, { recursive: true });
    fs.writeFileSync(path.join(hooks, "zmem-training-capture.py"),
        "import time\ntime.sleep(2)\nprint('{\"capture_id\":\"unexpected\",\"state\":\"partial\"}')\n", "utf8");
    const env = {
        ...process.env,
        ZMEM_ROOT: root,
        ZMEM_SESSION: "timeout",
        ZMEM_PYTHON: process.platform === "win32" ? "python" : "python3",
    };
    const started = Date.now();
    try {
        assert.deepStrictEqual(launch.runTrainingCapture(
            "claude", "recall", { session_id: "timeout", turn_id: "turn-timeout" },
            env, "start", {}, undefined, undefined, {
                deadline: 5000,
                now: () => 4300,
            }), {});
        const elapsed = Date.now() - started;
        assert(elapsed >= 100 && elapsed < 1500,
            `capture did not time out under its positive carried allocation: ${elapsed}ms`);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

test("convention capture observations remain detached", () => {
    const calls = [];
    const fakeChild = {
        stdin: {
            on() {},
            end(value) { calls.push({ input: value }); },
        },
        on(event) { calls.push({ event }); },
        kill() { calls.push({ killed: true }); },
        unref() { calls.push({ unref: true }); },
    };
    const env = { ...process.env, ZMEM_ROOT: REPO, ZMEM_SESSION: "convention" };
    const result = launch.runTrainingCapture(
        "claude", "convention-capture",
        { session_id: "convention", turn_id: "turn-convention" }, env,
        "observe", {}, undefined,
        (_python, args, options) => {
            calls.push({ args, options });
            return fakeChild;
        },
    );
    assert.deepStrictEqual(result, {});
    const spawnCall = calls.find((entry) => entry.args && entry.args[2] === "observe");
    assert(spawnCall);
    assert.strictEqual(spawnCall.options.windowsHide, true);
    assert.strictEqual(launch._trainingCaptureAction("recall"), "start");
    assert.strictEqual(launch._trainingCaptureAction("convention-capture"), "observe");
});

test("session-end uses the same canonical namespace as capture start", () => {
    const priorProject = process.env.CODEX_PROJECT_DIR;
    try {
        process.env.CODEX_PROJECT_DIR = REPO;
        launch.clearNamespaceCache();
        const meta = { session_id: "session-end-namespace", cwd: REPO };
        const started = launch.buildCanonicalEnv("codex", meta, "session-start");
        const cleared = launch.buildCanonicalEnv("codex", meta, "session-end", {
            requireResolvedNamespace: true,
        });
        assert(started.ZMEM_NAMESPACE);
        assert.strictEqual(cleared.ZMEM_NAMESPACE, started.ZMEM_NAMESPACE);
    } finally {
        if (priorProject === undefined) delete process.env.CODEX_PROJECT_DIR;
        else process.env.CODEX_PROJECT_DIR = priorProject;
        launch.clearNamespaceCache();
    }
});

test("detached observations start only after the translated hook has produced its payload", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-capture-order-"));
    const hooks = path.join(root, "hooks");
    const log = path.join(root, "capture-order.log");
    const capture = path.join(hooks, "lib", "zmem-training-capture.py");
    fs.mkdirSync(path.dirname(capture), { recursive: true });
    fs.writeFileSync(path.join(hooks, "zmem-convention-capture.sh"),
        "#!/usr/bin/env bash\n" +
        "printf 'child-start\\n' >> \"$ZMEM_CAPTURE_TEST_LOG\"\n" +
        "sleep 0.05\n" +
        "printf 'child-finished\\n' >> \"$ZMEM_CAPTURE_TEST_LOG\"\n" +
        "printf '<<<ZMEM_JSON>>>{\\\"additionalContext\\\":\\\"hook context\\\"}<<<END>>>\\n'\n",
        "utf8");
    fs.writeFileSync(capture,
        "import json, os, sys\n" +
        "payload = json.load(sys.stdin)\n" +
        "with open(os.environ['ZMEM_CAPTURE_TEST_LOG'], 'a', encoding='utf-8') as out:\n" +
        "    out.write('capture:' + payload['action'] + '\\n')\n" +
        "print('{}')\n",
        "utf8");
    const env = {
        ...process.env,
        PLUGIN_ROOT: root,
        ZMEM_ROOT: root,
        ZMEM_PYTHON: process.platform === "win32" ? "python" : "python3",
        ZMEM_CAPTURE_TEST_LOG: log,
        ZMEM_CAPTURE: "1",
        ZMEM_SESSION: "capture-order-session",
        ZMEM_NAMESPACE: "project:capture-order",
    };
    try {
        const result = spawnSync(process.execPath, [path.join(REPO, "hooks", "zmem-launch.js"),
            "convention-capture"], {
            input: JSON.stringify({
                session_id: "capture-order-session",
                namespace: "project:capture-order",
                task_id: "capture-order-task",
                turn_id: "capture-order-turn",
                cwd: root,
            }),
            env,
            encoding: "utf8",
            timeout: 5000,
        });
        assert.strictEqual(result.status, 0, result.stderr);
        const envelope = JSON.parse(result.stdout.trim());
        assert.strictEqual(envelope.hookSpecificOutput.hookEventName, "PostToolUse");
        const deadline = Date.now() + 1500;
        while ((!fs.existsSync(log) || !fs.readFileSync(log, "utf8").includes("capture:observe"))
               && Date.now() < deadline) {
            Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 20);
        }
        const order = fs.readFileSync(log, "utf8").trim().split(/\r?\n/);
        assert(order.includes("capture:observe"), `missing detached observation: ${order}`);
        assert(order.indexOf("child-finished") < order.indexOf("capture:observe"),
            `observation ran before child completion: ${order}`);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
    }
});

test("detached evidence writer hides its Windows launcher window", () => {
    const calls = [];
    const fakeChild = {
        stdin: {
            on() { return this; },
            end(input) { calls.push({ input }); },
        },
        on(event, handler) {
            calls.push({ event });
            if (event === "close") handler();
            return this;
        },
        kill() { calls.push({ killed: true }); },
    };
    const env = {
        ...process.env,
        ZMEM_ROOT: REPO,
        ZMEM_SESSION: "evidence-windows-hide",
    };
    const recorded = launch.recordEvidence(
        "claude", "convention-capture",
        { tool_name: "Edit", tool_input: { file_path: "C:/tmp/example.txt" } },
        { session_id: "evidence-windows-hide", task_id: "task-1", tool_call_id: "call-1" },
        env, () => "2026-01-01T00:00:00Z",
        (_python, args, options) => {
            calls.push({ args, options });
            return fakeChild;
        },
    );
    assert.strictEqual(recorded, true);
    const spawnCall = calls.find((entry) => entry.args && entry.args[1] === "evidence");
    assert(spawnCall);
    assert.strictEqual(spawnCall.args[2], "write");
    assert.strictEqual(spawnCall.options.detached, true);
    assert.strictEqual(spawnCall.options.windowsHide, true);
});

test("capture failure leaves the host response path unchanged", () => {
    const env = { ...process.env, ZMEM_ROOT: REPO, ZMEM_SESSION: "fail-open" };
    const meta = { session_id: "fail-open", namespace: "project:hook-adapter" };
    const failingExec = () => { throw new Error("capture unavailable"); };
    assert.deepStrictEqual(
        launch.runTrainingCapture("claude", "recall", meta, env, "start", {}, failingExec),
        {},
    );
    const raw = "<<<ZMEM_JSON>>>{\"additionalContext\":\"host context\"}<<<END>>>";
    assert.deepStrictEqual(launch.translate(raw, "claude", "recall", 10000), {
        hookSpecificOutput: {
            hookEventName: "UserPromptSubmit",
            additionalContext: "host context",
        },
    });
});

test("unsupported hosts and incomplete delivery payloads are no-ops", () => {
    const calls = [];
    const env = { ...process.env, ZMEM_ROOT: REPO, ZMEM_SESSION: "noop" };
    const meta = { session_id: "noop", namespace: "project:hook-adapter" };
    const execFn = fakeExec(calls);
    assert.deepStrictEqual(launch.runTrainingCapture("hermes", "recall", meta, env,
        "start", {}, execFn), {});
    assert.deepStrictEqual(launch.snapshotTrainingDelivery("claude", "recall", meta,
        env, { effective_ops: ["ignored"] }, execFn), {});
    assert.strictEqual(calls.length, 0);
});

console.log(`\n${passed} training capture launcher tests passed`);
