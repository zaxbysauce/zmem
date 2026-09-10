#!/usr/bin/env node
// test_codex_adapter.js — Codex-first plugin metadata and launcher coverage.
//
// Covers:
//   - .codex-plugin/plugin.json + repo-local marketplace metadata
//   - hooks/hooks.codex.json supported-event wiring using PLUGIN_ROOT
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
        "ZMEM_HOST", "ZMEM_ROOT", "ZMEM_DATA", "ZMEM_STORE", "ZMEM_PROJECT", "ZMEM_SESSION",
        "ZMEM_TRANSCRIPT", "ZMEM_AGENT_TRANSCRIPT", "ZMEM_AGENT_TYPE",
        "ZMEM_AGENT_ID", "ZMEM_NAMESPACE", "ZMEM_SKILLS_DIRS",
        "ZMEM_TIER0", "ZMEM_CTX_BUDGET", "ZMEM_INJECT",
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
    const hooks = JSON.parse(fs.readFileSync(path.join(REPO, "hooks", "hooks.codex.json"), "utf8"));
    // Final-critic fix (PR #69 feedback round): pin ALL SEVEN tracked
    // version surfaces together — a partial bump leaves a stale
    // update-discovery surface (root marketplace) that hosts compare
    // against (README Upgrade section).
    const rootMarketplace = JSON.parse(
        fs.readFileSync(path.join(REPO, "marketplace.json"), "utf8")
    );
    const hermesYaml = fs.readFileSync(path.join(REPO, "hermes-plugin", "plugin.yaml"), "utf8");
    const hermesVersion = /version:\s*(\S+)/.exec(hermesYaml)[1];

    eq("plugin: name", plugin.name, "zmem");
    eq("plugin: skills path", plugin.skills, "./skills/");
    // The Codex manifest declares its hooks file explicitly (./hooks/hooks.codex.json)
    // rather than relying on the default-named hooks/hooks.json, which CC would
    // ALSO auto-load and double-register (#36 M12). The ./ prefix is load-bearing:
    // codex-cli 0.152.1+ silently ignores hooks whose manifest path does not start
    // with ./ relative to the plugin root (issue #108, RC2).
    eq("plugin: declares codex hooks file explicitly", plugin.hooks, "./hooks/hooks.codex.json");
    ok("plugin: author.name present", !!(plugin.author && plugin.author.name));
    ok("plugin: interface.displayName present", !!(plugin.interface && plugin.interface.displayName));
    ok("plugin: interface.category present", !!(plugin.interface && plugin.interface.category));

    eq("marketplace: root name", marketplace.name, "zaxbyhub-local");
    eq("marketplace: one plugin entry", marketplace.plugins.length, 1);
    eq("marketplace: version matches plugin", marketplace.plugins[0].version, plugin.version);
    eq("release: Claude plugin matches Codex", claudePlugin.version, plugin.version);
    eq("release: Claude marketplace matches Codex", claudeMarketplace.plugins[0].version, plugin.version);
    eq("release: ZCode plugin matches Codex", zcodePlugin.version, plugin.version);
    eq("release: root marketplace matches Codex", rootMarketplace.plugins[0].version, plugin.version);
    eq("release: hermes plugin.yaml matches Codex", hermesVersion, plugin.version);
    eq("marketplace: plugin source kind", marketplace.plugins[0].source.source, "local");
    eq("marketplace: plugin source path points at repo root", marketplace.plugins[0].source.path, "./");
    eq("marketplace: installation policy", marketplace.plugins[0].policy.installation, "AVAILABLE");
    eq("marketplace: authentication policy", marketplace.plugins[0].policy.authentication, "ON_INSTALL");
    eq("marketplace: category", marketplace.plugins[0].category, "Productivity");

    ok("hooks: PostToolUseFailure is absent on Codex", !hooks.hooks.PostToolUseFailure);
    // Issue #95: PostCompact stays unregistered until #118 defines the
    // shared handlers (upstream Codex PostCompact carries only
    // trigger:manual|auto — no compact_summary — and accepts no
    // additionalContext, so registering it now would be dead config).
    ok("hooks: PostCompact is absent on Codex (deferred to #118)",
        !hooks.hooks.PostCompact);
    for (const eventName of [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "Stop",
        "SubagentStart",
        "SubagentStop",
    ]) {
        ok(`hooks: ${eventName} present`, Array.isArray(hooks.hooks[eventName]));
    }
    eq("hooks: PreToolUse uses the dump-verified matcher",
        hooks.hooks.PreToolUse[0].matcher, "Bash|apply_patch");
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
        // Must be undefined (deleted) here: ZMEM_STORE outranks ZMEM_DATA in
        // host.resolve_store_path, and buildCanonicalEnv starts from
        // { ...process.env }, so an ambient value would defeat the sandbox.
        ZMEM_STORE: undefined,
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

console.log("\n[6] Codex envelope clamp (issue #95: upstream spills above 2,500 tokens)");

{
    // Upstream codex-rs spills hook output over DEFAULT_HOOK_OUTPUT_TOKEN_LIMIT
    // = 2,500 tokens (verified 2026-09-09, tag rust-v0.153.0). The launcher must
    // clamp the codex envelope to 8000 encoded chars (~2000 tokens at the
    // plugin's 4-chars/token estimator) EVEN WHEN the operator sets a huge
    // ZMEM_CTX_BUDGET — and the clamp must be codex-specific. PRR-001: a
    // large systemMessage must consume budget too (it is appended to the same
    // envelope), and a systemMessage that alone cannot fit is dropped
    // (fail-open) instead of guaranteed to spill. PRR-002: an operator budget
    // above the cap produces a stderr warning on codex.
    function runClampCase(host, sysMsgLen) {
        const tree = fs.mkdtempSync(path.join(TMP_ROOT, "clamp-"));
        const pluginRoot = path.join(tree, "plugin");
        fs.mkdirSync(path.join(pluginRoot, "hooks"), { recursive: true });
        const giant = "A".repeat(30000);
        const sysMsg = sysMsgLen > 0 ? "S".repeat(sysMsgLen) : "";
        const payloadObj = { additionalContext: giant };
        if (sysMsg) payloadObj.systemMessage = sysMsg;
        fs.writeFileSync(path.join(pluginRoot, "hooks", "zmem-recall.sh"),
            "#!/usr/bin/env bash\n" +
            "printf '<<<ZMEM_JSON>>>%s<<<END>>>\\n' " +
            "'" + JSON.stringify(payloadObj).replace(/'/g, "'\\''") + "'\n");
        const env = envWith({
            [hostVarName(host)]: pluginRoot,
            ZMEM_DATA: path.join(tree, "data"),
            ZMEM_CTX_BUDGET: "50000",
        });
        const proc = spawnSync("node", [LAUNCHER, "recall"], {
            input: JSON.stringify({ session_id: "clamp", cwd: tree }),
            env, encoding: "utf8", timeout: 60000,
        });
        let envelope = null;
        try { envelope = JSON.parse(proc.stdout.trim()); } catch (e) { /* */ }
        const result = {
            encoded: envelope ? Buffer.byteLength(JSON.stringify(envelope), "utf8") : -1,
            sysMsgPresent: !!(envelope && envelope.systemMessage),
            stderr: proc.stderr || "",
        };
        fs.rmSync(tree, { recursive: true, force: true });
        return result;
    }
    function hostVarName(host) {
        return host === "codex" ? "PLUGIN_ROOT" : "CLAUDE_PLUGIN_ROOT";
    }

    const codexPlain = runClampCase("codex", 0);
    ok("clamp: codex envelope stays <= CODEX_ENVELOPE_CAP_CHARS",
        codexPlain.encoded >= 0 && codexPlain.encoded <= launch.CODEX_ENVELOPE_CAP_CHARS,
        "encoded=" + codexPlain.encoded + " cap=" + launch.CODEX_ENVELOPE_CAP_CHARS);
    const claudePlain = runClampCase("claude", 0);
    ok("clamp: claude control is NOT clamped by the codex cap",
        claudePlain.encoded > launch.CODEX_ENVELOPE_CAP_CHARS,
        "encoded=" + claudePlain.encoded);

    // PRR-001: a modest systemMessage is PRESERVED and the total stays in cap.
    const codexSmallSys = runClampCase("codex", 200);
    ok("clamp: codex envelope with systemMessage stays <= cap",
        codexSmallSys.encoded >= 0 && codexSmallSys.encoded <= launch.CODEX_ENVELOPE_CAP_CHARS,
        "encoded=" + codexSmallSys.encoded);
    ok("clamp: modest systemMessage is preserved",
        codexSmallSys.sysMsgPresent, "systemMessage was dropped");

    // PRR-001: a systemMessage that alone cannot fit is DROPPED (fail-open).
    const codexGiantSys = runClampCase("codex", 30000);
    ok("clamp: un-fittable systemMessage is dropped, envelope <= cap",
        codexGiantSys.encoded >= 0 && codexGiantSys.encoded <= launch.CODEX_ENVELOPE_CAP_CHARS
            && !codexGiantSys.sysMsgPresent,
        "encoded=" + codexGiantSys.encoded + " sysMsgPresent=" + codexGiantSys.sysMsgPresent);

    // PRR-002: operator budget above the cap warns on stderr (codex only,
    // only when explicitly set).
    function runWarnCase(host, budgetValue) {
        const tree = fs.mkdtempSync(path.join(TMP_ROOT, "warn-"));
        const pluginRoot = path.join(tree, "plugin");
        fs.mkdirSync(path.join(pluginRoot, "hooks"), { recursive: true });
        fs.writeFileSync(path.join(pluginRoot, "hooks", "zmem-recall.sh"),
            "#!/usr/bin/env bash\nprintf '{}\\n'\n");
        const overrides = { [hostVarName(host)]: pluginRoot, ZMEM_DATA: path.join(tree, "data") };
        if (budgetValue !== null) overrides.ZMEM_CTX_BUDGET = budgetValue;
        const proc = spawnSync("node", [LAUNCHER, "recall"], {
            input: JSON.stringify({ session_id: "warn", cwd: tree }),
            env: envWith(overrides), encoding: "utf8", timeout: 60000,
        });
        const warned = (proc.stderr || "").indexOf("exceeds the codex envelope cap") !== -1;
        fs.rmSync(tree, { recursive: true, force: true });
        return warned;
    }
    ok("clamp: operator budget above cap warns on stderr (codex)",
        runWarnCase("codex", "20000"));
    ok("clamp: no warning when the budget is not operator-set (codex default 9000 > 8000)",
        !runWarnCase("codex", null));
}

console.log("\n[7] Codex registered pre-tool path (issue #95)");

{
    // Simulate exactly what the Codex host does: load hooks.codex.json, apply
    // the matcher (exact alternation for all-alnum/pipe strings — codex-rs
    // events/common.rs matches_matcher), then drive the real launcher chain
    // against a seeded scratch store.
    const codexHooks = JSON.parse(
        fs.readFileSync(path.join(REPO, "hooks", "hooks.codex.json"), "utf8")).hooks;
    const matcher = codexHooks.PreToolUse[0].matcher;
    function matchesMatcher(matcherStr, toolName) {
        if (matcherStr === undefined || matcherStr === "" || matcherStr === "*") return true;
        const exact = /^[A-Za-z0-9_|]+$/.test(matcherStr);
        if (exact) return matcherStr.split("|").some((c) => c === toolName);
        return new RegExp(matcherStr).test(toolName);
    }
    ok("matcher: Bash matches", matchesMatcher(matcher, "Bash"));
    ok("matcher: apply_patch matches", matchesMatcher(matcher, "apply_patch"));
    ok("matcher: MCP tool does not match", !matchesMatcher(matcher, "mcp__srv__tool"));
    ok("matcher: write_stdin does not match", !matchesMatcher(matcher, "write_stdin"));

    const dataDir = path.join(TMP, "pretool-data");
    const workdir = path.join(TMP, "pretool-workdir");
    fs.mkdirSync(dataDir, { recursive: true });
    fs.mkdirSync(workdir, { recursive: true });
    // Resolve the namespace EXACTLY like the launcher does (host.py
    // resolve_namespace on the payload cwd) so the seed lands where the
    // drive recalls.
    const nsProc = spawnSync(PYTHON, [
        "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); import host; " +
        "print(host.resolve_namespace(sys.argv[2]))",
        path.join(REPO, "skills", "memory", "scripts"),
        workdir,
    ], { encoding: "utf8", timeout: 60000 });
    const namespace = nsProc.stdout.trim().split("\n").filter(Boolean).pop();
    ok("pretool: namespace resolved", !!namespace, nsProc.stderr || nsProc.stdout);
    const marker = "zmem95-adapter-row";
    const storeEnv = envWith({
        ZMEM_DATA: dataDir,
        ZMEM_STORE: path.join(dataDir, "store.sqlite"),
    });
    const added = spawnSync(PYTHON, [
        STORE, "add",
        "--namespace", namespace,
        "--type", "lesson",
        "--content", "git stash pop on a foreign stash applies someone "
            + "else's changes - always verify git stash list before stash "
            + "pop (" + marker + ")",
        "--signal", "test",
        "--source-ref", "issue-95-adapter",
        "--json",
    ], { env: storeEnv, encoding: "utf8", timeout: 120000 });
    eq("pretool: seed succeeds", added.status, 0);

    // The lane's canonical hazard scenario: a shell command strongly
    // matching the seeded lesson (a patch stub is legitimately below-bar
    // and the lane stays silent).
    const payload = JSON.stringify({
        session_id: "codex-pretool",
        turn_id: "codex-pretool-turn",
        cwd: workdir,
        hook_event_name: "PreToolUse",
        tool_name: "Bash",
        tool_input: { command: "git stash pop" },
        tool_use_id: "exec-adapter",
    });
    const env = envWith({
        PLUGIN_ROOT: REPO,
        PLUGIN_DATA: path.join(TMP, "codex-plugin-data"),
        ZMEM_DATA: dataDir,
        ZMEM_STORE: path.join(dataDir, "store.sqlite"),
        ZMEM_CTX_BUDGET: "50000",
    });
    const drove = spawnSync("node", [LAUNCHER, "pretool-recall"], {
        input: payload, env, encoding: "utf8", timeout: 120000,
    });
    let envelope = null;
    try { envelope = JSON.parse(drove.stdout.trim()); } catch (e) { /* */ }
    ok("pretool: launcher emits a JSON envelope", envelope !== null, drove.stdout.slice(0, 200));
    eq("pretool: hookEventName is PreToolUse",
        envelope && envelope.hookSpecificOutput && envelope.hookSpecificOutput.hookEventName,
        "PreToolUse");
    ok("pretool: fence carries the seeded marker",
        !!(envelope && envelope.hookSpecificOutput &&
            (envelope.hookSpecificOutput.additionalContext || "").indexOf(marker) !== -1),
        "marker missing from fence");
    ok("pretool: surfacing-only (no decision fields)",
        !/(permissionDecision|"decision")/.test(JSON.stringify(envelope || {})));
    ok("pretool: encoded envelope within the codex cap",
        Buffer.byteLength(JSON.stringify(envelope || {}), "utf8")
            <= launch.CODEX_ENVELOPE_CAP_CHARS);

    // PreCompact leg: the ledger for the session must be cleared by the drive
    // (upstream drops additionalContext on PreCompact, so the clear IS the
    // payload on Codex).
    const crypto = require("crypto");
    // delivery_ledger._hashed_name truncates the sha256 hex to 32 chars and
    // stores one {"id", "ts"} entry per delivered row.
    const ledgerName = crypto.createHash("sha256")
        .update("codex-pretool", "utf8").digest("hex").slice(0, 32) + ".ledger";
    const opsDir = path.join(dataDir, "ops");
    fs.mkdirSync(opsDir, { recursive: true });
    fs.writeFileSync(path.join(opsDir, ledgerName), JSON.stringify({
        entries: [{ id: "seed-1", ts: Math.floor(Date.now() / 1000) }],
    }));
    const pcEnv = envWith({
        PLUGIN_ROOT: REPO,
        PLUGIN_DATA: path.join(TMP, "codex-plugin-data"),
        ZMEM_DATA: dataDir,
        ZMEM_STORE: path.join(dataDir, "store.sqlite"),
        ZMEM_SESSION: "codex-pretool",
    });
    const drovePc = spawnSync("node", [LAUNCHER, "precompact"], {
        input: JSON.stringify({
            session_id: "codex-pretool",
            cwd: workdir,
            hook_event_name: "PreCompact",
            trigger: "manual",
        }),
        env: pcEnv, encoding: "utf8", timeout: 120000,
    });
    let pcEnvelope = null;
    try { pcEnvelope = JSON.parse(drovePc.stdout.trim()); } catch (e) { /* */ }
    eq("precompact: hookEventName is PreCompact",
        pcEnvelope && pcEnvelope.hookSpecificOutput && pcEnvelope.hookSpecificOutput.hookEventName,
        "PreCompact");
    const ledgerAfter = path.join(opsDir, ledgerName);
    let cleared = true;
    if (fs.existsSync(ledgerAfter)) {
        try {
            const data = JSON.parse(fs.readFileSync(ledgerAfter, "utf8"));
            cleared = !data.entries || data.entries.length === 0;
        } catch (e) { cleared = false; }
    }
    ok("precompact: session delivery ledger cleared by the drive", cleared,
        "ledger still present at " + ledgerName);
}

try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* */ }

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
