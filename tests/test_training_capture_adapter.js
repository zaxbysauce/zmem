#!/usr/bin/env node
// Focused launcher adapter coverage for automatic partial capture.

"use strict";

const assert = require("assert");
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

test("launcher start capture is synchronous and uses the 1.2 second bound", () => {
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
    assert.strictEqual(calls[0].options.timeout, 1200);
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
});

test("recall start enforces the timeout when a helper runs long", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-capture-timeout-"));
    const hooks = path.join(root, "hooks", "lib");
    fs.mkdirSync(hooks, { recursive: true });
    fs.writeFileSync(path.join(hooks, "zmem-training-capture.py"),
        "import time\ntime.sleep(2)\nprint('{}')\n", "utf8");
    const env = { ...process.env, ZMEM_ROOT: root, ZMEM_SESSION: "timeout" };
    const started = Date.now();
    try {
        assert.deepStrictEqual(launch.runTrainingCapture(
            "claude", "recall", { session_id: "timeout", turn_id: "turn-timeout" },
            env, "start"), {});
        const elapsed = Date.now() - started;
        assert(elapsed < 2800, `capture exceeded timeout plus scheduler slack: ${elapsed}ms`);
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
    assert(calls.some((entry) => entry.args && entry.args[2] === "observe"));
    assert.strictEqual(launch._trainingCaptureAction("recall"), "start");
    assert.strictEqual(launch._trainingCaptureAction("convention-capture"), "observe");
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
