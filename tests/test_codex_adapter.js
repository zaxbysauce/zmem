#!/usr/bin/env node
// test_codex_adapter.js — Codex-first plugin metadata and launcher coverage.
//
// Covers:
//   - .codex-plugin/plugin.json + repo-local marketplace metadata
//   - hooks/hooks.json supported-event wiring using PLUGIN_ROOT
//   - Codex host precedence over Claude/ZCode compatibility vars
//   - Codex canonical env, event mapping, envelopes, and native Tier0
//   - stable PostToolUse failure capture, fail-open success path, loop guards
//   - noise preservation and sentinel-safe translation on the Codex lane

"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const LAUNCHER = path.join(REPO, "hooks", "zmem-launch.js");
const STORE = path.join(REPO, "skills", "memory", "scripts", "store.py");
const PYTHON = process.platform === "win32" ? "python" : "python3";
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

function parseJson(text) {
    try {
        return JSON.parse(text.trim());
    } catch {
        return null;
    }
}

function envWith(overrides) {
    const e = { ...process.env };
    for (const k of [
        "ZMEM_HOST", "ZMEM_ROOT", "ZMEM_DATA", "ZMEM_PROJECT", "ZMEM_SESSION",
        "ZMEM_TRANSCRIPT", "ZMEM_AGENT_TRANSCRIPT", "ZMEM_AGENT_TYPE",
        "ZMEM_AGENT_ID", "ZMEM_NAMESPACE", "ZMEM_SKILLS_DIRS",
        "ZMEM_TIER0", "ZMEM_CTX_BUDGET",
        "PLUGIN_ROOT", "PLUGIN_DATA", "CODEX_PROJECT_DIR",
        "CLAUDE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_DATA",
        "ZCODE_PLUGIN_ROOT", "ZCODE_PROJECT_DIR", "ZCODE_PLUGIN_DATA",
        "CLAUDE_SESSION_ID", "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
        "ZMEM_CONVENTION_INTERVAL",
    ]) {
        delete e[k];
    }
    return Object.assign(e, overrides);
}

function withProcessEnv(overrides, fn) {
    const saved = new Map();
    for (const key of Object.keys(overrides)) {
        saved.set(key, Object.prototype.hasOwnProperty.call(process.env, key) ? process.env[key] : undefined);
        if (overrides[key] === undefined) delete process.env[key];
        else process.env[key] = overrides[key];
    }
    try {
        return fn();
    } finally {
        for (const [key, value] of saved.entries()) {
            if (value === undefined) delete process.env[key];
            else process.env[key] = value;
        }
    }
}

function runLauncher(hook, payload, env) {
    return spawnSync("node", [LAUNCHER, hook], {
        input: payload,
        env,
        encoding: "utf8",
        timeout: 30000,
    });
}

const TMP_ROOT = path.join(REPO, ".tmp-tests");
fs.mkdirSync(TMP_ROOT, { recursive: true });
const TMP = fs.mkdtempSync(path.join(TMP_ROOT, "zmem-codex-"));

console.log("\n[1] Codex plugin metadata");

{
    const plugin = JSON.parse(fs.readFileSync(path.join(REPO, ".codex-plugin", "plugin.json"), "utf8"));
    const claudePlugin = JSON.parse(
        fs.readFileSync(path.join(REPO, ".claude-plugin", "plugin.json"), "utf8")
    );
    const claudeMarketplace = JSON.parse(
        fs.readFileSync(path.join(REPO, ".claude-plugin", "marketplace.json"), "utf8")
    );
    const zcodePlugin = JSON.parse(
        fs.readFileSync(path.join(REPO, ".zcode-plugin", "plugin.json"), "utf8")
    );
    const marketplace = JSON.parse(
        fs.readFileSync(path.join(REPO, ".agents", "plugins", "marketplace.json"), "utf8")
    );
    const hooks = JSON.parse(fs.readFileSync(path.join(REPO, "hooks", "hooks.json"), "utf8"));

    eq("plugin: name", plugin.name, "zmem");
    eq("plugin: skills path", plugin.skills, "./skills/");
    ok("plugin: omits unsupported manifest hooks field", !Object.prototype.hasOwnProperty.call(plugin, "hooks"));
    ok("plugin: author.name present", !!(plugin.author && plugin.author.name));
    ok("plugin: interface.displayName present", !!(plugin.interface && plugin.interface.displayName));
    ok("plugin: interface.category present", !!(plugin.interface && plugin.interface.category));

    eq("marketplace: root name", marketplace.name, "zaxbyhub-local");
    eq("marketplace: one plugin entry", marketplace.plugins.length, 1);
    eq("marketplace: version matches plugin", marketplace.plugins[0].version, plugin.version);
    eq("release: Codex version", plugin.version, "0.7.0");
    eq("release: Claude plugin matches Codex", claudePlugin.version, plugin.version);
    eq("release: Claude marketplace matches Codex", claudeMarketplace.plugins[0].version, plugin.version);
    eq("release: ZCode plugin matches Codex", zcodePlugin.version, plugin.version);
    eq("marketplace: plugin source kind", marketplace.plugins[0].source.source, "local");
    eq("marketplace: plugin source path points at repo root", marketplace.plugins[0].source.path, "./");
    eq("marketplace: installation policy", marketplace.plugins[0].policy.installation, "AVAILABLE");
    eq("marketplace: authentication policy", marketplace.plugins[0].policy.authentication, "ON_INSTALL");
    eq("marketplace: category", marketplace.plugins[0].category, "Productivity");

    ok("hooks: PostToolUseFailure is absent on Codex", !hooks.hooks.PostToolUseFailure);
    for (const eventName of [
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
        "SubagentStart",
        "SubagentStop",
    ]) {
        ok(`hooks: ${eventName} present`, Array.isArray(hooks.hooks[eventName]));
    }
    eq("hooks: PostToolUse has two entries", hooks.hooks.PostToolUse.length, 2);
    const postToolCommands = hooks.hooks.PostToolUse
        .flatMap((entry) => entry.hooks || [])
        .map((hook) => hook.command || "");
    ok("hooks: convention capture uses PLUGIN_ROOT",
        postToolCommands.some((command) => command.indexOf("${PLUGIN_ROOT}") !== -1 && command.indexOf("convention-capture") !== -1),
        JSON.stringify(postToolCommands));
    ok("hooks: capture-failure uses PLUGIN_ROOT",
        postToolCommands.some((command) => command.indexOf("${PLUGIN_ROOT}") !== -1 && command.indexOf("capture-failure") !== -1),
        JSON.stringify(postToolCommands));
}

console.log("\n[2] Codex host precedence, env, event mapping, and envelopes");

{
    eq("detectHost: PLUGIN_ROOT beats CLAUDE_PLUGIN_ROOT",
        withProcessEnv({ PLUGIN_ROOT: REPO, CLAUDE_PLUGIN_ROOT: "C:\\fake-claude" }, () => launch.detectHost()),
        "codex");
    eq("detectHost: PLUGIN_DATA beats ZCODE_PLUGIN_ROOT",
        withProcessEnv({ PLUGIN_ROOT: undefined, PLUGIN_DATA: path.join(TMP, "plugin-data"), ZCODE_PLUGIN_ROOT: "C:\\fake-zcode" },
            () => launch.detectHost()),
        "codex");

    const explicitData = path.join(TMP, "shared-zmem");
    const env = withProcessEnv(
        {
            PLUGIN_ROOT: REPO,
            PLUGIN_DATA: path.join(TMP, "codex-plugin-data"),
            ZMEM_DATA: explicitData,
        },
        () => launch.buildCanonicalEnv("codex", {
            cwd: "C:\\repo",
            session_id: "codex-sess",
            transcript_path: "C:\\repo\\tx.jsonl",
            agent_type: "coder",
        }, "recall")
    );
    eq("buildCanonicalEnv(codex): host", env.ZMEM_HOST, "codex");
    eq("buildCanonicalEnv(codex): root from PLUGIN_ROOT", env.ZMEM_ROOT, REPO);
    eq("buildCanonicalEnv(codex): explicit shared data wins", env.ZMEM_DATA, explicitData);
    const defaultData = withProcessEnv(
        {
            PLUGIN_ROOT: REPO,
            PLUGIN_DATA: path.join(TMP, "must-not-be-the-store"),
            ZMEM_DATA: undefined,
        },
        () => launch.buildCanonicalEnv("codex", { cwd: "C:\\repo" }, "recall").ZMEM_DATA
    );
    eq("buildCanonicalEnv(codex): PLUGIN_DATA cannot split the store",
        defaultData, path.join(require("os").homedir(), ".zmem"));
    eq("buildCanonicalEnv(codex): tier0 is native", env.ZMEM_TIER0, "native");
    eq("buildCanonicalEnv(codex): budget matches native host", env.ZMEM_CTX_BUDGET, "9000");
    ok("buildCanonicalEnv(codex): skills dirs include ~/.codex/skills",
        env.ZMEM_SKILLS_DIRS.indexOf(path.join(require("os").homedir(), ".codex", "skills")) !== -1,
        env.ZMEM_SKILLS_DIRS);

    eq("hookEventNameFor(codex, capture-failure) -> PostToolUse",
        launch.hookEventNameFor("codex", "capture-failure"), "PostToolUse");
    eq("hookEventNameFor(codex, subagent-reflect) -> SubagentStop",
        launch.hookEventNameFor("codex", "subagent-reflect"), "SubagentStop");

    const recallEnvelope = launch.makeEnvelope("codex", "recall", "remember this");
    eq("makeEnvelope(codex, recall): hookEventName",
        recallEnvelope.hookSpecificOutput && recallEnvelope.hookSpecificOutput.hookEventName,
        "UserPromptSubmit");
    eq("makeEnvelope(codex, recall): additionalContext",
        recallEnvelope.hookSpecificOutput && recallEnvelope.hookSpecificOutput.additionalContext,
        "remember this");

    const failureEnvelope = launch.makeEnvelope("codex", "capture-failure", "tool failed");
    eq("makeEnvelope(codex, capture-failure): PostToolUse event",
        failureEnvelope.hookSpecificOutput && failureEnvelope.hookSpecificOutput.hookEventName,
        "PostToolUse");
}

console.log("\n[3] Codex PostToolUse failure normalization");

{
    const normalized = launch.normalizeCodexFailurePayload({
        session_id: "sess-fail",
        tool_name: "Bash",
        tool_input: { command: "false" },
        status: "error",
        error: "Exit code 1",
    });
    eq("normalize failure: preserves session_id", normalized && normalized.session_id, "sess-fail");
    eq("normalize failure: preserves tool_name", normalized && normalized.tool_name, "Bash");
    eq("normalize failure: carries string error", normalized && normalized.error, "Exit code 1");

    const nested = launch.normalizeCodexFailurePayload({
        sessionId: "sess-nested",
        tool: { name: "Read", input: { file_path: "a.txt" } },
        result: { status: "failed", message: "permission denied" },
    });
    eq("normalize nested failure: sessionId -> session_id", nested && nested.session_id, "sess-nested");
    eq("normalize nested failure: tool.name -> tool_name", nested && nested.tool_name, "Read");
    eq("normalize nested failure: result.message becomes error",
        nested && nested.error, "permission denied");

    eq("normalize success: fail open with null",
        launch.normalizeCodexFailurePayload({
            session_id: "sess-ok",
            tool_name: "Read",
            status: "success",
        }),
        null);
}

console.log("\n[4] Codex e2e: noise preservation, stable failure capture, loop guards, sentinel safety");

{
    const dataDir = path.join(TMP, "data");
    fs.mkdirSync(dataDir, { recursive: true });

    // Noise preservation: translated codex hooks must emit one clean envelope.
    const noisyRoot = path.join(TMP, "noisy-root");
    fs.mkdirSync(path.join(noisyRoot, "hooks"), { recursive: true });
    fs.writeFileSync(path.join(noisyRoot, "hooks", "zmem-session-start.sh"),
        "#!/usr/bin/env bash\n" +
        "printf '<<<ZMEM_JSON>>>%s<<<END>>>\\n' '{\"additionalContext\":\"codex session memory\"}'\n" +
        "echo '[zmem] merged 2 memories'\n");
    const noisy = runLauncher("session-start",
        JSON.stringify({ session_id: "codex-noise", cwd: TMP, hook_event_name: "SessionStart" }),
        envWith({ PLUGIN_ROOT: noisyRoot, PLUGIN_DATA: path.join(TMP, "plugin-data"), ZMEM_DATA: dataDir }));
    const noisyObj = parseJson(noisy.stdout);
    ok("noise: stdout is valid JSON", noisyObj !== null, noisy.stdout.slice(0, 200));
    eq("noise: SessionStart envelope preserved",
        noisyObj && noisyObj.hookSpecificOutput && noisyObj.hookSpecificOutput.hookEventName,
        "SessionStart");
    ok("noise: no stray stdout leaked", noisy.stdout.indexOf("[zmem] merged") === -1, noisy.stdout);

    // Stable failure capture: Codex PostToolUse failures normalize into the
    // existing capture-failure hook and rewrap back to PostToolUse.
    const failPayload = JSON.stringify({
        session_id: "codex-failure",
        cwd: TMP,
        hook_event_name: "PostToolUse",
        tool_name: "Bash",
        tool_input: { command: "false" },
        status: "error",
        error: "Exit code 1",
    });
    const failed = runLauncher("capture-failure", failPayload, envWith({
        PLUGIN_ROOT: REPO,
        PLUGIN_DATA: path.join(TMP, "plugin-data"),
        ZMEM_DATA: dataDir,
    }));
    const failedObj = parseJson(failed.stdout);
    ok("capture-failure: stdout is valid JSON", failedObj !== null, failed.stdout.slice(0, 200));
    eq("capture-failure: Codex envelope stays on PostToolUse",
        failedObj && failedObj.hookSpecificOutput && failedObj.hookSpecificOutput.hookEventName,
        "PostToolUse");
    ok("capture-failure: prompt mentions auto-capture",
        !!(failedObj && failedObj.hookSpecificOutput &&
            /auto-capture/.test(failedObj.hookSpecificOutput.additionalContext || "")),
        failed.stdout.slice(0, 300));

    const successPayload = JSON.stringify({
        session_id: "codex-success",
        cwd: TMP,
        hook_event_name: "PostToolUse",
        tool_name: "Read",
        status: "success",
    });
    const success = runLauncher("capture-failure", successPayload, envWith({
        PLUGIN_ROOT: REPO,
        PLUGIN_DATA: path.join(TMP, "plugin-data"),
        ZMEM_DATA: dataDir,
    }));
    eq("capture-failure: success payload fails open to {}", success.stdout.trim(), "{}");

    const transcript = path.join(TMP, "codex-parent.jsonl");
    fs.writeFileSync(transcript, JSON.stringify({ type: "assistant", message: { role: "assistant", content: [] } }) + "\n");
    const reflected = runLauncher("reflect", JSON.stringify({
        session_id: "codex-stop",
        transcript_path: transcript,
        cwd: TMP,
        hook_event_name: "Stop",
        stop_hook_active: true,
    }), envWith({
        PLUGIN_ROOT: REPO,
        PLUGIN_DATA: path.join(TMP, "plugin-data"),
        ZMEM_DATA: dataDir,
    }));
    eq("reflect: codex loop guard emits {}", reflected.stdout.trim(), "{}");

    const translated = launch.translate(
        '<<<ZMEM_JSON>>>{"additionalContext":"SENTINEL_CANARY <<<ZMEM_JSON_NEUTRALIZED>>> <<<END_NEUTRALIZED>>>"}<<<END>>>',
        "codex",
        "recall",
        9000
    );
    eq("translate: codex sentinel-safe envelope event",
        translated && translated.hookSpecificOutput && translated.hookSpecificOutput.hookEventName,
        "UserPromptSubmit");
    ok("translate: neutralized sentinel tokens survive inside content",
        /ZMEM_JSON_NEUTRALIZED/.test(
            translated && translated.hookSpecificOutput && translated.hookSpecificOutput.additionalContext
        ),
        JSON.stringify(translated));
}

console.log("\n[5] Three-host shared-store round trip");

{
    const sharedData = path.join(TMP, "shared-store");
    const common = {
        ZMEM_DATA: sharedData,
        ZMEM_MODEL_AUTODOWNLOAD: "0",
        PLUGIN_ROOT: undefined,
        PLUGIN_DATA: undefined,
        CLAUDE_PLUGIN_ROOT: undefined,
        CLAUDE_PLUGIN_OPTION_STOREDIRECTORY: undefined,
        ZCODE_PLUGIN_ROOT: undefined,
    };
    const hostOverrides = {
        codex: { PLUGIN_ROOT: REPO, PLUGIN_DATA: path.join(TMP, "codex-plugin-data") },
        claude: { CLAUDE_PLUGIN_ROOT: REPO },
        zcode: { ZCODE_PLUGIN_ROOT: REPO },
    };
    const hostEnvs = {};
    for (const host of Object.keys(hostOverrides)) {
        hostEnvs[host] = withProcessEnv(
            { ...common, ...hostOverrides[host] },
            () => launch.buildCanonicalEnv(host, { cwd: REPO, session_id: `roundtrip-${host}` }, "recall")
        );
        hostEnvs[host].ZMEM_MODEL_AUTODOWNLOAD = "0";
        eq(`roundtrip/${host}: canonical physical store`, hostEnvs[host].ZMEM_DATA, sharedData);
    }

    const rows = {
        codex: "codex-origin shared-memory canary alpha",
        claude: "claude-origin shared-memory canary beta",
        zcode: "zcode-origin shared-memory canary gamma",
    };
    for (const [host, content] of Object.entries(rows)) {
        const added = spawnSync(PYTHON, [
            STORE, "add",
            "--namespace", "project:zmem-cross-host",
            "--type", "fact",
            "--content", content,
            "--source-ref", `host:${host}`,
            "--signal", "test",
            "--capture-mode", "reviewed",
        ], { env: hostEnvs[host], encoding: "utf8", timeout: 30000 });
        eq(`roundtrip/${host}: write succeeds`, added.status, 0);
    }

    for (const [reader, env] of Object.entries(hostEnvs)) {
        for (const [writer, content] of Object.entries(rows)) {
            const recalled = spawnSync(PYTHON, [
                STORE, "recall",
                "--namespace", "project:zmem-cross-host",
                "--query", content,
                "--no-bump",
            ], { env, encoding: "utf8", timeout: 30000 });
            ok(`roundtrip/${reader}: recalls ${writer} row`,
                recalled.status === 0 && recalled.stdout.includes(content),
                (recalled.stderr || recalled.stdout || "").slice(0, 300));
        }
    }
}

try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* */ }

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
