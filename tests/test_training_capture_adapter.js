#!/usr/bin/env node
// Focused launcher adapter coverage for automatic partial capture.

"use strict";

const assert = require("assert");
const path = require("path");

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

        const delivered = launch.snapshotTrainingDelivery(host, "recall", meta,
            env, { rendered: "exact rendered context", effective_ops: ["run tests"] }, execFn);
        assert.strictEqual(delivered.capture_id, "capture-1");
        assert.strictEqual(calls[1].args[2], "snapshot");
        assert.strictEqual(calls[1].input.rendered, "exact rendered context");
        assert.deepStrictEqual(calls[1].input.effective_ops, ["run tests"]);
    });
}

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
