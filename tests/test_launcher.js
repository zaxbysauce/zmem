#!/usr/bin/env node
// test_launcher.js — Phase 3 host-adapter tests for zmem-launch.js.
//
// Drives the REAL zmem-launch.js + zmem-session-start.sh / zmem-recall.sh
// against a TEMP ZMEM_DATA store (never the box store). Covers CRITIC BLOCKER 2:
//   - per-host envelope translation (claude → hookSpecificOutput, zcode → bare)
//   - payload survives stray consolidate-style stdout noise (sentinel extraction)
//   - canonical ZMEM_NAMESPACE == host.resolve_namespace(project) (P2/P3 gap)
//   - encoded-budget truncation stays <= budget
//   - stdin bytes replayed to the child verbatim
//   - no ~5s session-start stall
//
// Run: node tests/test_launcher.js   (exit 0 = all pass)

"use strict";

const { spawnSync, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const LAUNCHER = path.join(REPO, "hooks", "zmem-launch.js");
const STORE_PY = path.join(REPO, "skills", "memory", "scripts", "store.py");
const HOST_PY = path.join(REPO, "skills", "memory", "scripts", "host.py");
const launch = require(path.join(REPO, "hooks", "zmem-launch.js"));

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

function eq(name, actual, expected) {
    ok(name, actual === expected, `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

// --- helpers ---------------------------------------------------------------

// A clean env with all host/canonical vars stripped, plus overrides applied.
function envWith(overrides) {
    const e = { ...process.env };
    for (const k of [
        "ZMEM_HOST", "ZMEM_ROOT", "ZMEM_DATA", "ZMEM_PROJECT", "ZMEM_SESSION",
        "ZMEM_TRANSCRIPT", "ZMEM_AGENT_TRANSCRIPT", "ZMEM_AGENT_TYPE",
        "ZMEM_AGENT_ID", "ZMEM_NAMESPACE", "ZMEM_SKILLS_DIRS",
        "ZMEM_TIER0", "ZMEM_CTX_BUDGET",
        "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR",
        "ZCODE_PROJECT_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
        "CLAUDE_SESSION_ID",
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

function seed(dataDir, namespace, type, content, confidence) {
    execFileSync(
        PYTHON,
        [STORE_PY, "add", "--namespace", namespace, "--type", type,
            "--content", content, "--confidence", String(confidence), "--signal", "user"],
        { env: envWith({ ZMEM_DATA: dataDir }), encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }
    );
}

// --- temp workspace --------------------------------------------------------
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "zmem-p3-"));
const DATA = path.join(TMP, "data");
const PROJ = path.join(TMP, "proj");
fs.mkdirSync(DATA, { recursive: true });
fs.mkdirSync(PROJ, { recursive: true });

const NS = resolveNs(PROJ); // non-git dir → project:<abspath key>
seed(DATA, NS, "lesson", "Always run the launcher tests before pushing.", 0.9);
seed(DATA, NS, "fact", "The host adapter exports canonical ZMEM_ env.", 0.8);

const SESSION_PAYLOAD = JSON.stringify({
    session_id: "sess-p3",
    transcript_path: "C:\\tmp\\transcript.jsonl",
    cwd: PROJ,
    hook_event_name: "SessionStart",
    source: "startup",
});

console.log("\n[1] End-to-end envelope translation (real session-start.sh, seeded temp store)");

{
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ,
    }));
    let obj = null;
    try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
    ok("claude: stdout is valid JSON", obj !== null, r.stdout.slice(0, 200));
    ok("claude: has hookSpecificOutput", !!(obj && obj.hookSpecificOutput));
    eq("claude: hookEventName == SessionStart",
        obj && obj.hookSpecificOutput && obj.hookSpecificOutput.hookEventName, "SessionStart");
    ok("claude: additionalContext contains seeded row",
        !!(obj && obj.hookSpecificOutput &&
            /Always run the launcher tests/.test(obj.hookSpecificOutput.additionalContext)));
    ok("claude: NOT a bare additionalContext", !(obj && obj.additionalContext));
}

{
    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: DATA, ZCODE_PLUGIN_ROOT: REPO, ZCODE_PROJECT_DIR: PROJ,
    }));
    let obj = null;
    try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
    ok("zcode: stdout is valid JSON", obj !== null, r.stdout.slice(0, 200));
    ok("zcode: bare additionalContext (no hookSpecificOutput)",
        !!(obj && obj.additionalContext && !obj.hookSpecificOutput));
    ok("zcode: additionalContext contains seeded row",
        !!(obj && /canonical ZMEM_ env/.test(obj.additionalContext || "")));
}

console.log("\n[2] Payload survives stray consolidate-style stdout noise");

{
    // Wrapper plugin root: runs the REAL session-start.sh (via ZMEM_ROOT=repo),
    // then prints consolidate-style noise onto the SAME stdout stream after the
    // sentinel. host.py copied in so the launcher resolves the same namespace.
    const WROOT = path.join(TMP, "wrap");
    fs.mkdirSync(path.join(WROOT, "hooks"), { recursive: true });
    fs.mkdirSync(path.join(WROOT, "skills", "memory", "scripts"), { recursive: true });
    fs.copyFileSync(HOST_PY, path.join(WROOT, "skills", "memory", "scripts", "host.py"));
    const repoBash = REPO.replace(/\\/g, "/");
    const wrapper =
        "#!/usr/bin/env bash\n" +
        "set -u\n" +
        `export ZMEM_ROOT="${REPO.replace(/\\/g, "\\\\")}"\n` +
        `bash "${repoBash}/hooks/zmem-session-start.sh"\n` +
        "rc=$?\n" +
        'echo "[zmem] merged 9 memories into 3"\n' +
        'printf "stray non-json line\\n"\n' +
        "exit $rc\n";
    fs.writeFileSync(path.join(WROOT, "hooks", "zmem-session-start.sh"), wrapper);

    const r = runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: WROOT, CLAUDE_PROJECT_DIR: PROJ,
    }));
    let obj = null;
    try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
    ok("noise: launcher stdout is clean single JSON envelope", obj !== null, r.stdout.slice(0, 200));
    ok("noise: no [zmem] merged leaked into launcher stdout",
        !/\[zmem\] merged/.test(r.stdout));
    eq("noise: still SessionStart envelope",
        obj && obj.hookSpecificOutput && obj.hookSpecificOutput.hookEventName, "SessionStart");
    ok("noise: seeded row still present despite trailing noise",
        !!(obj && obj.hookSpecificOutput &&
            /Always run the launcher tests/.test(obj.hookSpecificOutput.additionalContext)));
}

console.log("\n[3] extractPayload — sentinel extraction robustness (unit)");

{
    ok("extract: noise bracketing sentinel",
        JSON.stringify(launch.extractPayload(
            'garbage\n<<<ZMEM_JSON>>>{"additionalContext":"x"}<<<END>>>\n[zmem] merged 3'
        )) === '{"additionalContext":"x"}');
    ok("extract: LAST complete pair wins",
        launch.extractPayload(
            '<<<ZMEM_JSON>>>{"additionalContext":"OLD"}<<<END>>>' +
            '<<<ZMEM_JSON>>>{"additionalContext":"NEW"}<<<END>>>'
        ).additionalContext === "NEW");
    ok("extract: trailing unterminated START ignored",
        launch.extractPayload(
            '<<<ZMEM_JSON>>>{"additionalContext":"real"}<<<END>>><<<ZMEM_JSON>>>{trunc'
        ).additionalContext === "real");
    eq("extract: missing sentinel → null", launch.extractPayload("no sentinel here"), null);
    eq("extract: invalid JSON in sentinel → null",
        launch.extractPayload("<<<ZMEM_JSON>>>{not json}<<<END>>>"), null);
}

console.log("\n[4] Canonical ZMEM_NAMESPACE == host.resolve_namespace(project)");

{
    // envdump helper plugin root (not a translated hook → passthrough stdout).
    const EROOT = path.join(TMP, "envroot");
    fs.mkdirSync(path.join(EROOT, "hooks"), { recursive: true });
    fs.mkdirSync(path.join(EROOT, "skills", "memory", "scripts"), { recursive: true });
    fs.copyFileSync(HOST_PY, path.join(EROOT, "skills", "memory", "scripts", "host.py"));
    const dump =
        "#!/usr/bin/env bash\n" +
        "set -u\n" +
        'INPUT="$(cat)"\n' +
        'echo "ZMEM_HOST=$ZMEM_HOST"\n' +
        'echo "ZMEM_NAMESPACE=$ZMEM_NAMESPACE"\n' +
        'echo "ZMEM_DATA=$ZMEM_DATA"\n' +
        'echo "ZMEM_PROJECT=$ZMEM_PROJECT"\n' +
        'echo "ZMEM_SESSION=$ZMEM_SESSION"\n' +
        'echo "ZMEM_TRANSCRIPT=$ZMEM_TRANSCRIPT"\n' +
        'echo "ZMEM_CTX_BUDGET=$ZMEM_CTX_BUDGET"\n' +
        'echo "ZMEM_TIER0=$ZMEM_TIER0"\n' +
        'printf "STDIN_B64=%s\\n" "$(printf %s "$INPUT" | base64 | tr -d "\\n")"\n';
    fs.writeFileSync(path.join(EROOT, "hooks", "zmem-envdump.sh"), dump);

    // Use the repo itself as the project — it is a git checkout, so the
    // namespace comes from the git remote (the interesting P2/P3 path).
    const weird = '{"session_id":"s\\u00e9","cwd":"' +
        REPO.replace(/\\/g, "\\\\") +
        '","transcript_path":"C:\\\\t\\\\x.jsonl","agent_type":"coder","emoji":"✅"}';

    const r = runLauncher("envdump", weird, envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: EROOT, CLAUDE_PROJECT_DIR: REPO,
    }));
    const lines = r.stdout.split(/\r?\n/);
    const kv = {};
    for (const l of lines) {
        const i = l.indexOf("=");
        if (i > 0) kv[l.slice(0, i)] = l.slice(i + 1);
    }
    eq("envdump: ZMEM_HOST", kv.ZMEM_HOST, "claude");
    // Phase 8: "envdump" is not in NEEDS_NAMESPACE (it's a synthetic test hook
    // name, not a real one), so the perf-skip leaves ZMEM_NAMESPACE empty here
    // — this is the load-bearing proof that non-consumers no longer pay the
    // python+git resolution cost. The "resolves to the git-remote key" proof
    // for an ACTUAL consumer lives in the NEEDS_NAMESPACE unit block below.
    eq("envdump: ZMEM_NAMESPACE is EMPTY (non-consumer, perf-skip)", kv.ZMEM_NAMESPACE, "");
    eq("envdump: ZMEM_TIER0 (claude→native)", kv.ZMEM_TIER0, "native");
    eq("envdump: ZMEM_CTX_BUDGET (claude→9000)", kv.ZMEM_CTX_BUDGET, "9000");
    eq("envdump: ZMEM_SESSION from stdin session_id", kv.ZMEM_SESSION, "sé");
    eq("envdump: ZMEM_TRANSCRIPT from stdin", kv.ZMEM_TRANSCRIPT, "C:\\t\\x.jsonl");

    // [5] stdin replayed verbatim: decode the child's captured base64.
    const replayed = Buffer.from(kv.STDIN_B64 || "", "base64").toString("utf8");
    eq("stdin: child received EXACT original bytes", replayed, weird);
}

console.log("\n[5] Encoded-budget truncation stays <= budget");

{
    // Unit: fitEnvelope on oversized content, claude budget 9000.
    const big = "X".repeat(50000) + '\n"quotes"\tand\ttabs\n'.repeat(500);
    const env = launch.fitEnvelope("claude", "recall", big, 9000);
    const encoded = JSON.stringify(env);
    ok("budget(unit): encoded envelope <= 9000", encoded.length <= 9000, "len=" + encoded.length);
    ok("budget(unit): truncation marker appended",
        /\[recall truncated\]/.test(env.hookSpecificOutput.additionalContext));
    ok("budget(unit): still a valid SessionStart-shaped claude envelope",
        env.hookSpecificOutput && env.hookSpecificOutput.hookEventName === "UserPromptSubmit");

    // End-to-end through the real launcher: a recall script that emits an
    // OVERSIZED sentinel payload (larger than the 9000 claude budget). The
    // launcher must extract, rewrap, and truncate the ENCODED envelope to fit.
    // (A real DB row can't reach this size: recall.sh caps each row at 300
    // chars × 5 rows, so the launcher's own budget clamp is exercised here.)
    const BROOT = path.join(TMP, "budgetroot");
    fs.mkdirSync(path.join(BROOT, "hooks"), { recursive: true });
    const bigPayload = "OVERSIZED " + "lorem ipsum dolor ".repeat(2000);
    fs.writeFileSync(path.join(BROOT, "hooks", "zmem-recall.sh"),
        "#!/usr/bin/env bash\n" +
        `printf '<<<ZMEM_JSON>>>%s<<<END>>>\\n' '{"additionalContext":"${bigPayload}"}'\n`);
    const r = runLauncher("recall", "{}", envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: BROOT, CLAUDE_PROJECT_DIR: PROJ,
    }));
    ok("budget(e2e): launcher stdout <= 9000", r.stdout.trim().length <= 9000,
        "len=" + r.stdout.trim().length);
    let obj = null;
    try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
    ok("budget(e2e): valid JSON envelope", obj !== null, r.stdout.slice(0, 120));
    ok("budget(e2e): UserPromptSubmit envelope with truncation marker",
        !!(obj && obj.hookSpecificOutput &&
            obj.hookSpecificOutput.hookEventName === "UserPromptSubmit" &&
            /\[recall truncated\]/.test(obj.hookSpecificOutput.additionalContext)));
}

console.log("\n[6] Passthrough hook keeps child exit code + no envelope rewrap");

{
    // A non-translated hook name (convention-capture) whose script prints noise:
    // launcher must pass stdout through verbatim and NOT wrap/replace with {}.
    const PROOT = path.join(TMP, "passroot");
    fs.mkdirSync(path.join(PROOT, "hooks"), { recursive: true });
    fs.writeFileSync(path.join(PROOT, "hooks", "zmem-convention-capture.sh"),
        "#!/usr/bin/env bash\necho 'convention-raw-output'\nexit 0\n");
    const r = runLauncher("convention-capture", "{}", envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: PROOT, CLAUDE_PROJECT_DIR: PROJ,
    }));
    ok("passthrough: raw child stdout preserved (not translated to {})",
        /convention-raw-output/.test(r.stdout));
    ok("passthrough: NOT wrapped in hookSpecificOutput", !/hookSpecificOutput/.test(r.stdout));
}

console.log("\n[7] Phase 5: reflect (Stop) + capture-failure (PostToolUseFailure) translation");

{
    // A minimal real CC transcript with one failed Bash tool call.
    const TRANSCRIPT = path.join(TMP, "p5-transcript.jsonl");
    fs.writeFileSync(TRANSCRIPT, [
        JSON.stringify({ type: "assistant", message: { role: "assistant", content: [
            { type: "tool_use", id: "tu1", name: "Bash", input: { command: "false" } }] } }),
        JSON.stringify({ type: "user", message: { role: "user", content: [
            { type: "tool_result", content: "Exit code 1", is_error: true, tool_use_id: "tu1" }] },
            toolUseResult: "Error: Exit code 1" }),
    ].join("\n") + "\n");

    const stopPayload = (stopActive) => JSON.stringify({
        session_id: "p5-sess", transcript_path: TRANSCRIPT, cwd: PROJ,
        hook_event_name: "Stop", stop_hook_active: stopActive,
    });

    // claude: reflect → hookSpecificOutput{Stop} carrying the failure prompt.
    {
        const r = runLauncher("reflect", stopPayload(false), envWith({
            ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ,
        }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("reflect/claude: valid JSON", obj !== null, r.stdout.slice(0, 200));
        eq("reflect/claude: hookEventName == Stop",
            obj && obj.hookSpecificOutput && obj.hookSpecificOutput.hookEventName, "Stop");
        ok("reflect/claude: prompt mentions failed tool call",
            !!(obj && obj.hookSpecificOutput &&
                /failed tool call/.test(obj.hookSpecificOutput.additionalContext)));
    }

    // loop guard: stop_hook_active true → {} (never contribute to a stop loop).
    {
        const r = runLauncher("reflect", stopPayload(true), envWith({
            ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ,
        }));
        eq("reflect/claude: loop guard emits {}", r.stdout.trim(), "{}");
    }

    // zcode: reflect → bare additionalContext.
    {
        const r = runLauncher("reflect", stopPayload(false), envWith({
            ZMEM_DATA: DATA, ZCODE_PLUGIN_ROOT: REPO, ZCODE_PROJECT_DIR: PROJ,
        }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("reflect/zcode: bare additionalContext",
            !!(obj && obj.additionalContext && !obj.hookSpecificOutput));
    }

    // capture-failure: claude, error as a STRING (the real CC shape).
    {
        const r = runLauncher("capture-failure",
            JSON.stringify({ session_id: "p5-cf-" + Date.now(), tool_name: "Bash",
                tool_input: { command: "false" }, error: "Exit code 1" }),
            envWith({ ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        eq("capture-failure/claude: hookEventName == PostToolUseFailure",
            obj && obj.hookSpecificOutput && obj.hookSpecificOutput.hookEventName, "PostToolUseFailure");
        ok("capture-failure/claude: prompt mentions auto-capture",
            !!(obj && obj.hookSpecificOutput &&
                /auto-capture/.test(obj.hookSpecificOutput.additionalContext)));
    }
}

console.log("\n[8] No ~5s session-start stall");

{
    const t0 = Date.now();
    runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ,
    }));
    const elapsed = Date.now() - t0;
    ok("timing: session-start returns in < 4000ms (no consolidate wait stall)",
        elapsed < 4000, elapsed + "ms");
}

console.log("\n[9] Phase 7: subagent-recall (SubagentStart) + subagent-reflect (SubagentStop)");

{
    // Fresh temp store seeded with a namespace-scoped row, a user:global bridge
    // row, and an UNRELATED namespace row that must NOT be recalled.
    const SDATA = path.join(TMP, "p7data");
    fs.mkdirSync(SDATA, { recursive: true });
    const SNS = resolveNs(PROJ); // non-git → project:<abspath>
    seed(SDATA, SNS, "lesson", "P7_SCOPED prefer ripgrep over grep here.", 0.9);
    seed(SDATA, "user:global", "convention", "P7_GLOBAL end commits with a trailer.", 0.9);
    seed(SDATA, "project:UNRELATED_NS", "fact", "P7_UNRELATED must not appear.", 0.9);

    const rcOf = (content) => {
        const out = execFileSync(PYTHON, ["-c",
            "import sqlite3,os,sys\n" +
            "c=sqlite3.connect(sys.argv[1])\n" +
            "r=c.execute(\"SELECT retrieval_count FROM memory WHERE content LIKE ?\",('%'+sys.argv[2]+'%',)).fetchone()\n" +
            "print(r[0] if r else -1)",
            path.join(SDATA, "store.sqlite"), content], { encoding: "utf8" }).trim();
        return parseInt(out, 10);
    };

    const startPayload = JSON.stringify({
        session_id: "p7-sess", transcript_path: "C:\\tmp\\parent.jsonl", cwd: PROJ,
        prompt_id: "pp1", agent_id: "agent777", agent_type: "coder",
        hook_event_name: "SubagentStart",
    });

    // subagent-recall / claude: SubagentStart envelope, scoped + bridge rows,
    // unrelated excluded, and READ-ONLY (retrieval_count unchanged).
    {
        const r = runLauncher("subagent-recall", startPayload, envWith({
            ZMEM_DATA: SDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ,
        }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("subagent-recall/claude: valid JSON", obj !== null, r.stdout.slice(0, 200));
        eq("subagent-recall/claude: hookEventName == SubagentStart",
            obj && obj.hookSpecificOutput && obj.hookSpecificOutput.hookEventName, "SubagentStart");
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("subagent-recall: scoped namespace row present", /P7_SCOPED/.test(ac));
        ok("subagent-recall: user:global bridge row present", /P7_GLOBAL/.test(ac));
        ok("subagent-recall: unrelated namespace row absent", !/P7_UNRELATED/.test(ac));
        ok("subagent-recall: agent_type in header", /agent coder/.test(ac));
        eq("subagent-recall: READ-ONLY — scoped row rc unchanged (0)", rcOf("P7_SCOPED"), 0);
        eq("subagent-recall: READ-ONLY — global row rc unchanged (0)", rcOf("P7_GLOBAL"), 0);
    }

    // subagent-recall / zcode: bare additionalContext.
    {
        const r = runLauncher("subagent-recall", startPayload, envWith({
            ZMEM_DATA: SDATA, ZCODE_PLUGIN_ROOT: REPO, ZCODE_PROJECT_DIR: PROJ,
        }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("subagent-recall/zcode: bare additionalContext (no hookSpecificOutput)",
            !!(obj && obj.additionalContext && !obj.hookSpecificOutput));
    }

    // --- subagent-reflect: scans the SUBAGENT's own transcript ---------------
    // agent_transcript_path has a failed tool call; the PARENT transcript_path
    // has NONE — proving reflect reads agent_transcript_path, not the parent.
    const AGENT_TX = path.join(TMP, "p7-agent.jsonl");
    fs.writeFileSync(AGENT_TX, [
        JSON.stringify({ type: "assistant", message: { role: "assistant", content: [
            { type: "tool_use", id: "atu1", name: "Bash", input: { command: "npm test" } }] } }),
        JSON.stringify({ type: "user", message: { role: "user", content: [
            { type: "tool_result", content: "Exit code 1: tests failed", is_error: true, tool_use_id: "atu1" }] },
            toolUseResult: "Error: Exit code 1" }),
    ].join("\n") + "\n");
    const PARENT_TX = path.join(TMP, "p7-parent.jsonl"); // no failures
    fs.writeFileSync(PARENT_TX, JSON.stringify({ type: "assistant",
        message: { role: "assistant", content: [{ type: "text", text: "done" }] } }) + "\n");

    const stopPayload = (opts) => JSON.stringify(Object.assign({
        session_id: "p7-sess", transcript_path: PARENT_TX, cwd: PROJ,
        agent_id: "agent777", agent_type: "coder",
        hook_event_name: "SubagentStop", stop_hook_active: false,
        agent_transcript_path: AGENT_TX,
    }, opts || {}));

    // claude: subagent-reflect → SubagentStop envelope with a failure prompt
    // sourced from agent_transcript_path.
    {
        const r = runLauncher("subagent-reflect", stopPayload(), envWith({
            ZMEM_DATA: SDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ,
        }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("subagent-reflect/claude: valid JSON", obj !== null, r.stdout.slice(0, 200));
        eq("subagent-reflect/claude: hookEventName == SubagentStop",
            obj && obj.hookSpecificOutput && obj.hookSpecificOutput.hookEventName, "SubagentStop");
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("subagent-reflect: detected the subagent's failed tool call",
            /failed tool call/.test(ac) && /Bash/.test(ac));
        ok("subagent-reflect: per-subagent source-ref (session+agent)",
            /session:p7-sess:agent:agent777/.test(ac));
    }

    // loop guard: stop_hook_active true → {} (never contribute to a subagent stop loop).
    {
        const r = runLauncher("subagent-reflect", stopPayload({ stop_hook_active: true }),
            envWith({ ZMEM_DATA: SDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ }));
        eq("subagent-reflect: loop guard emits {}", r.stdout.trim(), "{}");
    }

    // no agent_transcript_path → {} (never fall back to the parent transcript).
    {
        const r = runLauncher("subagent-reflect",
            stopPayload({ agent_transcript_path: undefined }),
            envWith({ ZMEM_DATA: SDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ }));
        eq("subagent-reflect: no agent transcript → {}", r.stdout.trim(), "{}");
    }

    // per-subagent dedup: seed a lesson for agent777, re-fire → {} for agent777,
    // but a sibling agent999 (same session, own transcript) still reflects.
    {
        const NS2 = resolveNs(PROJ);
        execFileSync(PYTHON, [STORE_PY, "add", "--namespace", NS2, "--type", "lesson",
            "--content", "captured for agent777", "--source-ref", "session:p7-sess:agent:agent777"],
            { env: envWith({ ZMEM_DATA: SDATA }), encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
        const r1 = runLauncher("subagent-reflect", stopPayload(), envWith({
            ZMEM_DATA: SDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ }));
        eq("subagent-reflect: dedup suppresses re-reflection for same agent", r1.stdout.trim(), "{}");

        const siblingTx = path.join(TMP, "p7-agent999.jsonl");
        fs.copyFileSync(AGENT_TX, siblingTx);
        const r2 = runLauncher("subagent-reflect",
            stopPayload({ agent_id: "agent999", agent_transcript_path: siblingTx }),
            envWith({ ZMEM_DATA: SDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ }));
        let obj = null; try { obj = JSON.parse(r2.stdout.trim()); } catch (e) { /* */ }
        ok("subagent-reflect: sibling agent still reflects (per-subagent dedup)",
            !!(obj && obj.hookSpecificOutput &&
                /session:p7-sess:agent:agent999/.test(obj.hookSpecificOutput.additionalContext || "")));
    }
}

console.log("\n[10] Phase 7 unit: buildCanonicalEnv exports agent transcript + id");

{
    const meta = {
        session_id: "u-sess", cwd: "C:\\proj",
        transcript_path: "C:\\parent.jsonl",
        agent_transcript_path: "C:\\subagents\\agent-x.jsonl",
        agent_type: "reviewer", agent_id: "agentXYZ",
    };
    const saved = process.env.CLAUDE_PLUGIN_ROOT;
    process.env.CLAUDE_PLUGIN_ROOT = REPO;
    const env = launch.buildCanonicalEnv("claude", meta);
    if (saved === undefined) delete process.env.CLAUDE_PLUGIN_ROOT; else process.env.CLAUDE_PLUGIN_ROOT = saved;
    eq("buildCanonicalEnv: ZMEM_AGENT_TRANSCRIPT from agent_transcript_path",
        env.ZMEM_AGENT_TRANSCRIPT, "C:\\subagents\\agent-x.jsonl");
    eq("buildCanonicalEnv: ZMEM_TRANSCRIPT stays the parent transcript",
        env.ZMEM_TRANSCRIPT, "C:\\parent.jsonl");
    eq("buildCanonicalEnv: ZMEM_AGENT_ID from agent_id", env.ZMEM_AGENT_ID, "agentXYZ");
    eq("buildCanonicalEnv: ZMEM_AGENT_TYPE from agent_type", env.ZMEM_AGENT_TYPE, "reviewer");
}

// TRANSLATED_HOOKS + EVENT_MAP wiring (unit).
{
    ok("TRANSLATED_HOOKS includes subagent-recall", launch.TRANSLATED_HOOKS.has("subagent-recall"));
    ok("TRANSLATED_HOOKS includes subagent-reflect", launch.TRANSLATED_HOOKS.has("subagent-reflect"));
    eq("EVENT_MAP subagent-recall → SubagentStart", launch.EVENT_MAP["subagent-recall"], "SubagentStart");
    eq("EVENT_MAP subagent-reflect → SubagentStop", launch.EVENT_MAP["subagent-reflect"], "SubagentStop");
}

console.log("\n[11] Phase 8 PERF: ZMEM_NAMESPACE resolved only for NEEDS_NAMESPACE consumers");

{
    // grep -l ZMEM_NAMESPACE hooks/*.sh (Phase 8 evidence) showed every
    // translated hook EXCEPT convention-capture consumes it. Assert the set
    // directly, and assert it is NOT the same object identity as
    // TRANSLATED_HOOKS even though membership coincides today (per the
    // launcher's own comment: these answer different questions).
    const expectedConsumers = [
        "session-start", "recall", "subagent-recall", "reflect",
        "capture-failure", "subagent-reflect",
    ];
    for (const h of expectedConsumers) {
        ok(`NEEDS_NAMESPACE includes ${h}`, launch.NEEDS_NAMESPACE.has(h));
    }
    ok("NEEDS_NAMESPACE excludes convention-capture (per-edit, high-frequency, computes its own NS_HINT)",
        !launch.NEEDS_NAMESPACE.has("convention-capture"));

    const saved = process.env.CLAUDE_PLUGIN_ROOT;
    process.env.CLAUDE_PLUGIN_ROOT = REPO;
    const metaRepo = { cwd: REPO };
    const expectedNs = resolveNs(REPO);

    // Consumers still resolve to the real derived git-remote key.
    for (const h of ["session-start", "recall", "subagent-recall", "reflect",
        "capture-failure", "subagent-reflect"]) {
        const env = launch.buildCanonicalEnv("claude", metaRepo, h);
        eq(`buildCanonicalEnv(${h}): ZMEM_NAMESPACE == resolve_namespace(repo)`,
            env.ZMEM_NAMESPACE, expectedNs);
    }
    // The non-consumer: resolution is skipped entirely (empty, not attempted).
    {
        const env = launch.buildCanonicalEnv("claude", metaRepo, "convention-capture");
        eq("buildCanonicalEnv(convention-capture): ZMEM_NAMESPACE is EMPTY (perf-skip)",
            env.ZMEM_NAMESPACE, "");
    }
    // Back-compat: no hookName arg at all still resolves (fail toward
    // correctness, never silently toward speed, for an unrecognized caller).
    {
        const env = launch.buildCanonicalEnv("claude", metaRepo);
        eq("buildCanonicalEnv(no hookName): still resolves (safe default)",
            env.ZMEM_NAMESPACE, expectedNs);
    }
    if (saved === undefined) delete process.env.CLAUDE_PLUGIN_ROOT; else process.env.CLAUDE_PLUGIN_ROOT = saved;

    // Rough before/after latency: convention-capture (skip) vs session-start's
    // consumer path (resolve) via buildCanonicalEnv directly, averaged over a
    // few calls (unit-level — isolates the resolution cost from bash/store.py
    // startup noise that dominates a full end-to-end hook timing).
    process.env.CLAUDE_PLUGIN_ROOT = REPO;
    const N = 5;
    let tResolve = 0, tSkip = 0;
    for (let i = 0; i < N; i++) {
        let t0 = Date.now();
        launch.buildCanonicalEnv("claude", metaRepo, "session-start");
        tResolve += Date.now() - t0;
        t0 = Date.now();
        launch.buildCanonicalEnv("claude", metaRepo, "convention-capture");
        tSkip += Date.now() - t0;
    }
    if (saved === undefined) delete process.env.CLAUDE_PLUGIN_ROOT; else process.env.CLAUDE_PLUGIN_ROOT = saved;
    const avgResolve = tResolve / N, avgSkip = tSkip / N;
    console.log(`  INFO  avg buildCanonicalEnv latency: session-start(resolves)=${avgResolve.toFixed(1)}ms ` +
        `convention-capture(skips)=${avgSkip.toFixed(1)}ms`);
    ok("perf: convention-capture (skip) is faster than session-start (resolve)",
        avgSkip < avgResolve, `resolve=${avgResolve.toFixed(1)}ms skip=${avgSkip.toFixed(1)}ms`);
}

// --- cleanup + report ------------------------------------------------------
try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* */ }

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
