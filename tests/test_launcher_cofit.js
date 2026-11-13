#!/usr/bin/env node
// test_launcher_cofit.js — content/systemMessage co-fit at the launcher
// (trace AC3+AC4, extending the issue #154 completed-envelope byte-cap block
// in tests/test_codex_adapter.js).
//
// Covers:
//   - AC3: when translated content and the operator systemMessage cannot
//     co-fit at the full budget but CAN co-fit with the content re-fitted at
//     (budget - the message's encoded marginal size), translate() attaches
//     the message AND returns the re-fitted content, with the completed
//     envelope satisfying encodedSize(result) <= budget (the #154 invariant).
//   - AC4: when that co-fit retry would leave content below a usefulness
//     floor (content truncated below the floor), translate() keeps the
//     full-budget content fit and drops the message (content-wins preserved).
//   - fail-open preservation: a message-only payload that alone cannot fit
//     the budget drops the message, returning {} or a minimal envelope.
//   - the #154 invariant encodedEnvelopeSize(result) <= budget on every
//     branch above.
//
// Pure-helper coverage only (translate/fitEnvelope/makeEnvelope/encodedSize
// as exported by hooks/zmem-launch.js); no store, no child processes.
//
// Run: node tests/test_launcher_cofit.js   (exit nonzero on any failure)

"use strict";

// Pin a hermetic store BEFORE the #244 guard import: the guard fires on any
// box with a real default store when the require precedes the pin (CI is
// silent). This file spawns no store child at all — the pin only keeps the
// guard's resolved path out of the real ~/.zmem area.
const fs = require("fs");
const os = require("os");
const path = require("path");
{
    const guardScratch = fs.mkdtempSync(path.join(os.tmpdir(),
        "guard-cofit-"));
    process.env.ZMEM_STORE = path.join(guardScratch, "store.sqlite");
    process.env.ZMEM_DATA = guardScratch;
    delete process.env.ZMEM_ALLOW_REAL_STORE;
}
require("./_store_guard.js"); // issue #244 isolation guard

const REPO = path.resolve(__dirname, "..");
const LAUNCHER = path.join(REPO, "hooks", "zmem-launch.js");
const launch = require(LAUNCHER);

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
    ok(name, actual === expected, `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

// --- helpers (copied from the issue #154 block in tests/test_codex_adapter.js)

function sentinelPayload(payload) {
    return "<<<ZMEM_JSON>>>" + JSON.stringify(payload) + "<<<END>>>";
}

function exactSystemMessage(host, hookName, targetBytes) {
    for (let n = 0; n < targetBytes; n++) {
        const msg = "x".repeat(n);
        const bytes = launch.encodedSize(launch.makeEnvelope(host, hookName, "", msg));
        if (bytes === targetBytes) return msg;
    }
    throw new Error("no exact system-message length");
}

function exactMarginalSystemMessage(host, hookName, marginalBytes) {
    const base = launch.encodedSize(launch.makeEnvelope(host, hookName, ""));
    for (let n = 0; n < marginalBytes; n++) {
        const msg = "x".repeat(n);
        const bytes = launch.encodedSize(launch.makeEnvelope(host, hookName, "", msg));
        if (bytes - base === marginalBytes) return msg;
    }
    throw new Error("no exact system-message marginal");
}

function encodedEnvelopeSize(envelope) {
    return Buffer.byteLength(JSON.stringify(envelope), "utf8");
}

function envelopeContent(env) {
    if (!env) return undefined;
    if (env.hookSpecificOutput) return env.hookSpecificOutput.additionalContext;
    return env.additionalContext;
}

const BUDGET = 8000; // codex cap (launch.CODEX_ENVELOPE_CAP_BYTES)

// Every leg records its translate() result here; the invariant leg (d)
// re-checks encodedEnvelopeSize(result) <= budget over all of them.
const branchResults = [];

// --- AC3: co-fit retry attaches the message and re-fits the content -------
//
// Verified window at budget 8000: a 7900-char content fits the FULL budget
// (content-only envelope 7982 bytes) but cannot co-fit a marginal-180
// message (8162 bytes); content re-fitted at budget-180 co-fits with the
// message at exactly 8000 bytes (~7737-char content + truncation marker).
// RED at base: translate() drops the message whenever the full-budget
// content fit + message overflows — no co-fit retry exists.

function testCoFitRetryAttachesMessageAndReFitsContent() {
    const msg = exactMarginalSystemMessage("codex", "recall", 180);
    const content = "c".repeat(7900);
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        "codex", "recall", BUDGET
    );
    branchResults.push({ label: "ac3/co-fit-retry", env, budget: BUDGET });
    ok("ac3: systemMessage attached", env.systemMessage !== undefined,
        "systemMessage was dropped (no co-fit retry at budget - marginal)");
    ok("ac3: completed envelope within budget (#154 invariant)",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
    const ctx = envelopeContent(env);
    const ctxLen = typeof ctx === "string" ? ctx.length : String(ctx);
    ok("ac3: re-fitted content attached (non-stub, >1000 chars)",
        typeof ctx === "string" && ctx.length > 1000,
        "additionalContext length=" + ctxLen);
}

// --- AC4: content-wins preserved below the usefulness floor ---------------
//
// A marginal-7990 message leaves a re-fit budget of 10 bytes — no content
// survives it — so the retry must be rejected: the full-budget content fit
// is kept and the message dropped. Pinned by comparing the result with
// fitEnvelope(content, full budget) directly. (GREEN at base by design: this
// leg guards against the AC3 fix over-reaching.)

function testContentWinsWhenRetryFallsBelowUsefulnessFloor() {
    const msg = exactMarginalSystemMessage("codex", "recall", 7990);
    // 7900 chars fit the full 8000 budget verbatim (no truncation marker),
    // so "full-length retained" is assertable as exact content equality.
    const content = "c".repeat(7900);
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        "codex", "recall", BUDGET
    );
    branchResults.push({ label: "ac4/content-wins", env, budget: BUDGET });
    ok("ac4: systemMessage dropped", !env.systemMessage,
        "systemMessage was retained");
    const ctx = envelopeContent(env);
    const ctxLen = typeof ctx === "string" ? ctx.length : String(ctx);
    ok("ac4: full-budget content fit retained verbatim",
        typeof ctx === "string" && ctx === content,
        "additionalContext length=" + ctxLen
        + " (expected the full " + content.length + "-char content)");
    ok("ac4: completed envelope within budget (#154 invariant)",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
    const full = launch.fitEnvelope("codex", "recall", content, BUDGET);
    eq("ac4: envelope equals the full-budget content fit",
        JSON.stringify(env), JSON.stringify(full));
}

// --- fail-open preservation: message-only over budget ---------------------

function testMessageOnlyOverBudgetFailsOpen() {
    const msg = exactSystemMessage("codex", "recall", 8001);
    const env = launch.translate(
        sentinelPayload({ systemMessage: msg }), "codex", "recall", BUDGET
    );
    branchResults.push({ label: "fail-open/message-only", env, budget: BUDGET });
    ok("fail-open: systemMessage dropped", !env.systemMessage,
        "systemMessage was retained");
    ok("fail-open: completed envelope within budget",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
    const ctx = envelopeContent(env);
    const minimal = JSON.stringify(env) === "{}"
        || typeof ctx !== "string" || ctx === "";
    ok("fail-open: result is {} or a minimal envelope", minimal,
        "unexpectedly rich envelope: "
        + JSON.stringify(env).slice(0, 80));
}

// --- AC3 host spread: the co-fit retry is host-independent -----------------
//
// claude/zcode budgets are 9000 (BUDGET_DEFAULT) and SessionStart is the
// operator-notice hook there; codex stays covered by the 8000/recall leg
// above and is re-driven at 9000/SessionStart for the spread. Verified
// window at budget 9000: 8900-char content fits the full budget on every
// host (envelope base: codex/claude 78, zcode 24) but cannot co-fit a
// marginal-180 message; content re-fitted at 9000-180 co-fits (~8700+ chars
// of content — comfortably above any usefulness floor).

function assertCoFitRetryAtBudget(host, hookName, budget, contentLen, marginal) {
    const msg = exactMarginalSystemMessage(host, hookName, marginal);
    const content = "c".repeat(contentLen);
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        host, hookName, budget
    );
    const tag = "ac3[" + host + "/" + hookName + " @" + budget + "]";
    branchResults.push({ label: "ac3/" + host + "-" + hookName + "@" + budget, env, budget });
    ok(tag + ": systemMessage attached", env.systemMessage !== undefined,
        "systemMessage was dropped (no co-fit retry at budget - marginal)");
    ok(tag + ": completed envelope within budget",
        encodedEnvelopeSize(env) <= budget,
        "encoded=" + encodedEnvelopeSize(env));
    const ctx = envelopeContent(env);
    const ctxLen = typeof ctx === "string" ? ctx.length : String(ctx);
    ok(tag + ": re-fitted content attached (>1000 chars)",
        typeof ctx === "string" && ctx.length > 1000,
        "additionalContext length=" + ctxLen);
}

function testHostSpreadCoFitRetry() {
    assertCoFitRetryAtBudget("codex", "SessionStart", 9000, 8900, 180);
    assertCoFitRetryAtBudget("claude", "SessionStart", 9000, 8900, 180);
    assertCoFitRetryAtBudget("zcode", "SessionStart", 9000, 8900, 180);
}

// --- floor-boundary legs (amendment 3b) -------------------------------------
//
// The usefulness floor is pinned by BEHAVIOR at comfortable distances, not
// by reading any constant: a retry that leaves ~2000 chars of content
// (comfortably above any sane floor) must ATTACH the message; a retry that
// leaves only ~50-70 chars, or the bare truncation marker, must DROP it and
// keep the full-budget content fit. All sizes are derived numerically from
// the exported makeEnvelope/encodedSize pair (base envelope overhead), the
// same technique as the legs above.

const FLOOR_HOST = "codex";
const FLOOR_HOOK = "recall";
const FLOOR_BASE = launch.encodedSize(
    launch.makeEnvelope(FLOOR_HOST, FLOOR_HOOK, ""));
// Full-budget content: fits verbatim with 100 bytes of headroom (envelope
// = budget - 100), so the drop legs can assert exact content equality.
const FLOOR_FULL_LEN = BUDGET - FLOOR_BASE - 100;
// Retry lands at a base+2050 envelope -> ~2049 chars of re-fitted content
// (comfortably ABOVE the floor) -> message must attach.
const FLOOR_MARGINAL_ABOVE = BUDGET - FLOOR_BASE - 2050;
// Retry lands at a base+70 envelope -> ~69 chars of re-fitted content
// (comfortably BELOW the floor) -> message must drop, content kept.
const FLOOR_MARGINAL_BELOW = BUDGET - FLOOR_BASE - 70;
// Retry budget is EXACTLY the marker-only envelope (empty prefix +
// "\n[recall truncated]") -> the fit is marker-only -> message must drop.
const FLOOR_MARGINAL_MARKER_ONLY = BUDGET - launch.encodedSize(
    launch.makeEnvelope(FLOOR_HOST, FLOOR_HOOK, "[recall truncated]"));

function floorBoundaryContent() {
    return "c".repeat(FLOOR_FULL_LEN);
}

function testRetryAboveFloorAttachesMessage() {
    const msg = exactMarginalSystemMessage(FLOOR_HOST, FLOOR_HOOK,
        FLOOR_MARGINAL_ABOVE);
    const content = floorBoundaryContent();
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        FLOOR_HOST, FLOOR_HOOK, BUDGET
    );
    branchResults.push({ label: "floor/attach-above", env, budget: BUDGET });
    ok("floor/above: systemMessage attached (retry keeps ~2000 chars)",
        env.systemMessage !== undefined, "systemMessage was dropped");
    ok("floor/above: completed envelope within budget",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
    const ctx = envelopeContent(env);
    const ctxLen = typeof ctx === "string" ? ctx.length : String(ctx);
    ok("floor/above: re-fitted content is substantial (>1000 chars)",
        typeof ctx === "string" && ctx.length > 1000,
        "additionalContext length=" + ctxLen);
}

function testRetryBelowFloorDropsMessageAndKeepsFullContent() {
    const msg = exactMarginalSystemMessage(FLOOR_HOST, FLOOR_HOOK,
        FLOOR_MARGINAL_BELOW);
    const content = floorBoundaryContent();
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        FLOOR_HOST, FLOOR_HOOK, BUDGET
    );
    branchResults.push({ label: "floor/drop-below", env, budget: BUDGET });
    ok("floor/below: systemMessage dropped (retry leaves ~50 chars)",
        !env.systemMessage, "systemMessage was retained");
    const ctx = envelopeContent(env);
    const ctxLen = typeof ctx === "string" ? ctx.length : String(ctx);
    ok("floor/below: full-budget content fit retained verbatim",
        typeof ctx === "string" && ctx === content,
        "additionalContext length=" + ctxLen
        + " (expected the full " + content.length + "-char content)");
    ok("floor/below: completed envelope within budget",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
}

function testRetryMarkerOnlyDropsMessage() {
    // The retry budget equals the marker-only envelope exactly, so the best
    // re-fit is the bare truncation marker — a useless floor outcome.
    const msg = exactMarginalSystemMessage(FLOOR_HOST, FLOOR_HOOK,
        FLOOR_MARGINAL_MARKER_ONLY);
    const content = floorBoundaryContent();
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        FLOOR_HOST, FLOOR_HOOK, BUDGET
    );
    branchResults.push({ label: "floor/marker-only", env, budget: BUDGET });
    ok("floor/marker-only: systemMessage dropped (retry is marker-only)",
        !env.systemMessage, "systemMessage was retained");
    const ctx = envelopeContent(env);
    const ctxLen = typeof ctx === "string" ? ctx.length : String(ctx);
    ok("floor/marker-only: full-budget content fit retained verbatim",
        typeof ctx === "string" && ctx === content,
        "additionalContext length=" + ctxLen
        + " (expected the full " + content.length + "-char content)");
    ok("floor/marker-only: completed envelope within budget",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
}

// --- degenerate retry budget (amendment 3c) ---------------------------------
//
// A message whose marginal >= the whole budget leaves a retry budget of
// zero or less: no crash, the message drops, the full-budget content fit
// stays within the cap.

function testDegenerateRetryBudgetNoCrash() {
    const msg = exactMarginalSystemMessage(FLOOR_HOST, FLOOR_HOOK, BUDGET);
    const content = floorBoundaryContent();
    let env;
    try {
        env = launch.translate(
            sentinelPayload({ additionalContext: content, systemMessage: msg }),
            FLOOR_HOST, FLOOR_HOOK, BUDGET
        );
    } catch (e) {
        ok("degenerate/retry-budget<=0: translate does not throw", false,
            "threw: " + e.message);
        return;
    }
    branchResults.push({ label: "degenerate/huge-marginal", env, budget: BUDGET });
    ok("degenerate/retry-budget<=0: translate does not throw",
        typeof env === "object" && env !== null,
        "result=" + JSON.stringify(env));
    ok("degenerate/retry-budget<=0: systemMessage dropped",
        !env.systemMessage, "systemMessage was retained");
    const ctx = envelopeContent(env);
    const ctxLen = typeof ctx === "string" ? ctx.length : String(ctx);
    ok("degenerate/retry-budget<=0: full-budget content fit retained verbatim",
        typeof ctx === "string" && ctx === content,
        "additionalContext length=" + ctxLen
        + " (expected the full " + content.length + "-char content)");
    ok("degenerate/retry-budget<=0: completed envelope within budget",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
}

// --- retry-envelope guard (amendment 3d) ------------------------------------
//
// On the co-fit branch specifically: the completed envelope is within the
// budget, the message is attached, and the inner content is a real payload —
// never empty and never just the truncation marker.

function testRetryEnvelopeGuard() {
    const msg = exactMarginalSystemMessage("codex", "recall", 180);
    const content = "c".repeat(7900);
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        "codex", "recall", BUDGET
    );
    branchResults.push({ label: "guard/co-fit-branch", env, budget: BUDGET });
    ok("guard: systemMessage attached", env.systemMessage !== undefined,
        "systemMessage was dropped");
    ok("guard: encodedEnvelopeSize(result) <= budget",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
    const ctx = envelopeContent(env);
    ok("guard: inner content is not empty",
        typeof ctx === "string" && ctx.length > 0,
        "additionalContext=" + JSON.stringify(ctx));
    ok("guard: inner content is not marker-only",
        typeof ctx === "string" && ctx.trim() !== "[recall truncated]",
        "additionalContext=" + JSON.stringify(
            typeof ctx === "string" ? ctx.slice(0, 60) : ctx));
}

// --- removed-C4 scenario (round-2 amendment) --------------------------------
//
// The superseded test_codex_adapter.js saturation case, re-pinned post-#213:
// 30000-char content + a 200-char message at the 8000 codex cap. The guarded
// co-fit retry must ATTACH the message and re-fit the content: truncation
// marker present, content strictly shorter than the full-budget content-only
// fit (the retry gave bytes back to the message), envelope within the cap.
// RED at base (the message is dropped today).

function testRemovedC4SaturationScenario() {
    const msg = "x".repeat(200);
    const content = "c".repeat(30000);
    const env = launch.translate(
        sentinelPayload({ additionalContext: content, systemMessage: msg }),
        "codex", "recall", BUDGET
    );
    branchResults.push({ label: "removedC4/saturation", env, budget: BUDGET });
    ok("removedC4: systemMessage attached", env.systemMessage !== undefined,
        "systemMessage was dropped");
    const ctx = envelopeContent(env);
    const fullOnly = envelopeContent(
        launch.fitEnvelope("codex", "recall", content, BUDGET));
    ok("removedC4: re-fitted content carries the truncation marker",
        typeof ctx === "string" && ctx.indexOf("[recall truncated]") !== -1,
        "additionalContext tail=" + JSON.stringify(
            typeof ctx === "string" ? ctx.slice(-40) : ctx));
    ok("removedC4: content strictly shorter than the full-budget fit",
        typeof ctx === "string" && typeof fullOnly === "string"
        && ctx.length < fullOnly.length,
        "refit=" + (typeof ctx === "string" ? ctx.length : String(ctx))
        + " fullOnly=" + (typeof fullOnly === "string" ? fullOnly.length
                                                      : String(fullOnly)));
    ok("removedC4: completed envelope within budget",
        encodedEnvelopeSize(env) <= BUDGET,
        "encoded=" + encodedEnvelopeSize(env));
}

// --- (d) the #154 invariant on every branch above --------------------------

function testEveryBranchSatisfiesEncodedBudgetInvariant() {
    ok("invariant: branch results recorded (every leg ran)",
        branchResults.length >= 3, "recorded=" + branchResults.length);
    for (const r of branchResults) {
        const b = r.budget || BUDGET;
        ok("invariant[" + r.label + "]: encodedEnvelopeSize(result) <= " + b,
            encodedEnvelopeSize(r.env) <= b,
            "encoded=" + encodedEnvelopeSize(r.env));
    }
}

console.log("[co-fit] content/systemMessage co-fit at the launcher (AC3+AC4)");

testCoFitRetryAttachesMessageAndReFitsContent();
testContentWinsWhenRetryFallsBelowUsefulnessFloor();
testMessageOnlyOverBudgetFailsOpen();
testHostSpreadCoFitRetry();
testRetryAboveFloorAttachesMessage();
testRetryBelowFloorDropsMessageAndKeepsFullContent();
testRetryMarkerOnlyDropsMessage();
testDegenerateRetryBudgetNoCrash();
testRetryEnvelopeGuard();
testRemovedC4SaturationScenario();
testEveryBranchSatisfiesEncodedBudgetInvariant();

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
