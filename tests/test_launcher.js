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
        "PLUGIN_ROOT", "PLUGIN_DATA", "CODEX_PROJECT_DIR",
        "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "CLAUDE_PROJECT_DIR",
        "ZCODE_PROJECT_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
        "CLAUDE_SESSION_ID", "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
        "ZMEM_CONVENTION_INTERVAL",
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

// Render a value exactly as the hooks do when interpolating it into the
// suggested `store.py add ...` command — i.e. through shlex.quote.
//
// Do NOT assume a value is quote-free just because it looks benign: whether
// shlex.quote adds quotes depends on the CONTENT, and a test fixture does not
// control that. A GitHub Windows runner's temp dir resolves to an 8.3 short
// path (`C:\Users\RUNNER~1\...`); `~` is shell-special, so the namespace
// derived from it comes back single-quoted there while the identical
// assertion passes unquoted on a developer box. Asking Python for the answer
// keeps the expectation right for whatever the path happens to be.
function shquote(s) {
    return execFileSync(
        PYTHON,
        ["-c", "import shlex,sys; sys.stdout.write(shlex.quote(sys.argv[1]))", s],
        { encoding: "utf8" }
    );
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
const TMP_ROOT = path.join(REPO, ".tmp-tests");
fs.mkdirSync(TMP_ROOT, { recursive: true });
const TMP = fs.mkdtempSync(path.join(TMP_ROOT, "zmem-p3-"));
const DATA = path.join(TMP, "data");
const PROJ = path.join(TMP, "proj");
fs.mkdirSync(DATA, { recursive: true });
fs.mkdirSync(PROJ, { recursive: true });

console.log("\n[0] Git-for-Windows child PATH derivation");

if (process.platform === "win32") {
    const fakeGitRoot = path.join(TMP, "PortableGit");
    const fakeUsrBin = path.join(fakeGitRoot, "usr", "bin");
    const fakeGitBin = path.join(fakeGitRoot, "bin");
    fs.mkdirSync(fakeUsrBin, { recursive: true });
    fs.mkdirSync(fakeGitBin, { recursive: true });
    const fakeBash = path.join(fakeUsrBin, "bash.exe");
    fs.writeFileSync(fakeBash, "");
    const child = launch.buildChildEnv({ PATH: "C:\\Windows" }, fakeBash);
    const pathParts = child.PATH.split(path.delimiter);
    ok("child PATH contains Git bin", pathParts.includes(fakeGitBin), child.PATH);
    ok(
        "child PATH avoids usr/usr/bin",
        !pathParts.includes(path.join(fakeGitRoot, "usr", "usr", "bin")),
        child.PATH
    );

    const directGitRoot = path.join(TMP, "PortableGit-direct");
    const directGitBin = path.join(directGitRoot, "bin");
    const directUsrBin = path.join(directGitRoot, "usr", "bin");
    fs.mkdirSync(directGitBin, { recursive: true });
    fs.mkdirSync(directUsrBin, { recursive: true });
    const directBash = path.join(directGitBin, "bash.exe");
    fs.writeFileSync(directBash, "");
    const directChild = launch.buildChildEnv({ PATH: "C:\\Windows" }, directBash);
    const directParts = directChild.PATH.split(path.delimiter);
    ok("direct Git bin PATH contains usr/bin", directParts.includes(directUsrBin), directChild.PATH);
    ok("direct Git bin PATH keeps bin", directParts.includes(directGitBin), directChild.PATH);
}

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
    // A non-translated hook name whose script prints noise: launcher must pass
    // stdout through verbatim and NOT wrap/replace with {}.
    //
    // NOTE: this block used to use "convention-capture" as its vehicle.
    // convention-capture is now a TRANSLATED hook — it emits the sentinel and
    // needs the Claude Code hookSpecificOutput rewrap to be injected at all
    // (see block [12]) — so the passthrough contract is asserted with a
    // synthetic hook name instead. Coverage is unchanged; only the vehicle moved.
    const PROOT = path.join(TMP, "passroot");
    fs.mkdirSync(path.join(PROOT, "hooks"), { recursive: true });
    fs.writeFileSync(path.join(PROOT, "hooks", "zmem-passthru.sh"),
        "#!/usr/bin/env bash\necho 'convention-raw-output'\nexit 0\n");
    const r = runLauncher("passthru", "{}", envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: PROOT, CLAUDE_PROJECT_DIR: PROJ,
    }));
    ok("passthrough: raw child stdout preserved (not translated to {})",
        /convention-raw-output/.test(r.stdout));
    ok("passthrough: NOT wrapped in hookSpecificOutput", !/hookSpecificOutput/.test(r.stdout));

    // A non-translated hook whose child script exits nonzero: launcher must
    // pass the child's real exit code through, not swallow/normalize it.
    const PROOT2 = path.join(TMP, "passroot-exit");
    fs.mkdirSync(path.join(PROOT2, "hooks"), { recursive: true });
    fs.writeFileSync(path.join(PROOT2, "hooks", "zmem-passthru.sh"),
        "#!/usr/bin/env bash\necho 'convention-raw-output'\nexit 3\n");
    const r2 = runLauncher("passthru", "{}", envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: PROOT2, CLAUDE_PROJECT_DIR: PROJ,
    }));
    ok("passthrough: nonzero child exit code passed through",
        r2.status === 3, "status=" + r2.status);
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
    // Every hook whose script reads $ZMEM_NAMESPACE. convention-capture is
    // BACK in this set (reversing part of the Phase 8 skip): it renders a
    // `store.py add --namespace …` suggestion, and its old basename-derived
    // NS_HINT pointed at a namespace the unified recall path never queries, so
    // captured conventions were invisible to the shared store. Correctness
    // beats the ~100ms saving on the per-edit path.
    const expectedConsumers = [
        "session-start", "recall", "subagent-recall", "reflect",
        "capture-failure", "subagent-reflect", "convention-capture",
    ];
    for (const h of expectedConsumers) {
        ok(`NEEDS_NAMESPACE includes ${h}`, launch.NEEDS_NAMESPACE.has(h));
    }
    // An unrecognized hook name is still skipped (the perf mechanism itself is
    // intact — only convention-capture's membership changed).
    ok("NEEDS_NAMESPACE excludes an unrecognized hook name (perf-skip mechanism intact)",
        !launch.NEEDS_NAMESPACE.has("envdump"));

    const saved = process.env.CLAUDE_PLUGIN_ROOT;
    process.env.CLAUDE_PLUGIN_ROOT = REPO;
    const metaRepo = { cwd: REPO };
    const expectedNs = resolveNs(REPO);

    // Consumers still resolve to the real derived git-remote key.
    for (const h of expectedConsumers) {
        const env = launch.buildCanonicalEnv("claude", metaRepo, h);
        eq(`buildCanonicalEnv(${h}): ZMEM_NAMESPACE == resolve_namespace(repo)`,
            env.ZMEM_NAMESPACE, expectedNs);
    }
    // A non-consumer: resolution is skipped entirely (empty, not attempted).
    {
        const env = launch.buildCanonicalEnv("claude", metaRepo, "envdump");
        eq("buildCanonicalEnv(envdump): ZMEM_NAMESPACE is EMPTY (perf-skip)",
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

    // Rough before/after latency: a non-consumer (skip) vs session-start's
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
        launch.buildCanonicalEnv("claude", metaRepo, "envdump");
        tSkip += Date.now() - t0;
    }
    if (saved === undefined) delete process.env.CLAUDE_PLUGIN_ROOT; else process.env.CLAUDE_PLUGIN_ROOT = saved;
    const avgResolve = tResolve / N, avgSkip = tSkip / N;
    console.log(`  INFO  avg buildCanonicalEnv latency: session-start(resolves)=${avgResolve.toFixed(1)}ms ` +
        `envdump(skips)=${avgSkip.toFixed(1)}ms (informational only - see behavioral ` +
        `ZMEM_NAMESPACE-emptiness assertions above for the deterministic skip-vs-resolve check)`);
    // NOTE: a strict wall-clock `avgSkip < avgResolve` assertion used to live
    // here, but comparing two coarse averages with no margin is flaky under
    // CI load (shared/throttled runners can make this fail spuriously even
    // when the underlying behavior is correct). The actual behavioral claim -
    // a non-consumer skips namespace resolution while session-start performs
    // it - is already asserted deterministically above via ZMEM_NAMESPACE
    // emptiness, so timing is logged for visibility only and is not asserted on.
}

console.log("\n[12] convention-capture is TRANSLATED and namespace-aware (was silently dead on CC)");

{
    // Drives the REAL hooks/zmem-convention-capture.sh through the REAL
    // launcher against a temp store + temp project. Two bugs are covered:
    //   (a) envelope: it emits a bare {additionalContext}; Claude Code only
    //       honors hookSpecificOutput.additionalContext, so before the fix the
    //       capture prompt was passed through verbatim and NEVER injected.
    //   (b) namespace: it used to render a basename-derived NS_HINT into the
    //       suggested `store.py add --namespace …`, pointing captured
    //       conventions at a namespace no recall path queries.
    const CDATA = path.join(TMP, "ccdata");
    fs.mkdirSync(CDATA, { recursive: true });
    // Seed once so the store (and its `meta` table, which the turn counter
    // increments) exists.
    seed(CDATA, "user:global", "fact", "seed row to create the store schema.", 0.9);
    const CNS = resolveNs(PROJ);

    const ccPayload = (sid) => JSON.stringify({
        session_id: sid, cwd: PROJ, hook_event_name: "PostToolUse",
        tool_name: "Edit", tool_input: { file_path: "a.txt" },
    });
    // INTERVAL=1 → fires on the first call instead of the tenth.
    const ccEnv = (extra) => envWith(Object.assign({
        ZMEM_DATA: CDATA, ZMEM_CONVENTION_INTERVAL: "1", CLAUDE_PROJECT_DIR: PROJ,
    }, extra));

    // --- claude host -------------------------------------------------------
    {
        const r = runLauncher("convention-capture", ccPayload("cc-claude-" + Date.now()),
            ccEnv({ CLAUDE_PLUGIN_ROOT: REPO }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("convention-capture/claude: stdout is a single valid JSON envelope",
            obj !== null, r.stdout.slice(0, 300));
        ok("convention-capture/claude: no raw sentinel leaked to the runner",
            !/<<<ZMEM_JSON>>>/.test(r.stdout));
        eq("convention-capture/claude: hookEventName == PostToolUse",
            obj && obj.hookSpecificOutput && obj.hookSpecificOutput.hookEventName, "PostToolUse");
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("convention-capture/claude: carries the capture prompt",
            /ZMem convention capture/.test(ac), ac.slice(0, 200));
        // Rendering is shlex.quote()'d (PR#10 round-3 fix: repo-controlled
        // ns_hint/store_py_hint/session_id could otherwise break out of the
        // suggested shell command). Compare against the SAME quoting rather
        // than assuming CNS is quote-free — on a Windows CI runner the temp
        // path is an 8.3 short name containing `~`, which shlex.quote quotes.
        ok("convention-capture/claude: suggests the CANONICAL namespace",
            ac.indexOf('--namespace ' + shquote(CNS)) !== -1, ac.slice(0, 400));
        ok("convention-capture/claude: does NOT suggest the legacy basename namespace",
            !/--namespace project:proj\b/.test(ac));
        console.log("  EVIDENCE(claude) " + JSON.stringify(obj).slice(0, 420));
    }

    // --- zcode host --------------------------------------------------------
    {
        const r = runLauncher("convention-capture", ccPayload("cc-zcode-" + Date.now()),
            ccEnv({ ZCODE_PLUGIN_ROOT: REPO, ZCODE_PROJECT_DIR: PROJ }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("convention-capture/zcode: bare additionalContext (no hookSpecificOutput)",
            !!(obj && obj.additionalContext && !obj.hookSpecificOutput), r.stdout.slice(0, 300));
        const ac = (obj && obj.additionalContext) || "";
        ok("convention-capture/zcode: suggests the CANONICAL namespace",
            ac.indexOf('--namespace ' + shquote(CNS)) !== -1, ac.slice(0, 400));
        console.log("  EVIDENCE(zcode)  " + JSON.stringify(obj).slice(0, 420));
    }

    // Non-convention tool (Read) → still a well-formed empty envelope, never
    // stray bytes on the now-buffered stdout.
    {
        const r = runLauncher("convention-capture",
            JSON.stringify({ session_id: "cc-skip", cwd: PROJ, tool_name: "Read", tool_input: {} }),
            ccEnv({ CLAUDE_PLUGIN_ROOT: REPO }));
        eq("convention-capture: non-convention tool → {}", r.stdout.trim(), "{}");
    }

    // Wiring (unit).
    ok("TRANSLATED_HOOKS includes convention-capture",
        launch.TRANSLATED_HOOKS.has("convention-capture"));
    eq("EVENT_MAP convention-capture → PostToolUse",
        launch.EVENT_MAP["convention-capture"], "PostToolUse");

    // --- injection: a hostile `origin` remote must not escape the rendered
    // suggested command (PR#10 round-3 finding #2) ---------------------------
    // ns_hint is derived from the project's git remote — repository-controlled.
    // host.py's _normalize_remote() only special-cases recognizable URL shapes
    // (scp-like `user@host:path`, `scheme://host/path`); anything else falls
    // through its "unrecognized shape" branch UNCHANGED (just lowercased), so
    // a hostile origin containing quotes/$(...)/backticks reaches this hook's
    // rendering verbatim. Prove that with a REAL git repo + REAL remote +
    // the REAL launcher, not a hand-crafted env var.
    {
        const GITPROJ = path.join(TMP, "hostile-git-proj");
        fs.mkdirSync(GITPROJ, { recursive: true });
        const CANARY = path.join(GITPROJ, "PWNED_CANARY");
        const HOSTILE_REMOTE = '"; $(touch ' + CANARY.replace(/\\/g, "/") + '); echo "';
        execFileSync("git", ["init", "-q"], { cwd: GITPROJ });
        execFileSync("git", ["remote", "add", "origin", HOSTILE_REMOTE], { cwd: GITPROJ });

        // Sanity: confirm the hostile string actually reaches resolve_namespace()
        // unescaped — otherwise this test would pass for the wrong reason.
        const hostileNs = resolveNs(GITPROJ);
        ok("injection: hostile origin survives into the resolved namespace (sanity)",
            hostileNs.indexOf('"') !== -1 && hostileNs.indexOf("$(") !== -1, hostileNs);

        const HDATA = path.join(TMP, "hostile-data");
        fs.mkdirSync(HDATA, { recursive: true });
        seed(HDATA, "user:global", "fact", "seed row to create the store schema.", 0.9);

        const r = runLauncher("convention-capture",
            JSON.stringify({
                session_id: "cc-hostile-" + Date.now(), cwd: GITPROJ,
                hook_event_name: "PostToolUse", tool_name: "Edit", tool_input: { file_path: "a.txt" },
            }),
            envWith({
                ZMEM_DATA: HDATA, ZMEM_CONVENTION_INTERVAL: "1",
                CLAUDE_PROJECT_DIR: GITPROJ, CLAUDE_PLUGIN_ROOT: REPO,
            }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("injection: hook still renders a well-formed capture prompt",
            /ZMem convention capture/.test(ac), ac.slice(0, 200));

        // Pull the suggested command out of its backtick fence and actually
        // run it through bash, exactly as an agent copy-pasting the suggestion
        // would. Fixed: the malicious content is shlex.quote()'d, so it is
        // inert single-quoted text as far as bash is concerned. Broken (pre-fix):
        // bash would expand $(touch ...) while parsing the command line, and
        // the canary file would exist BEFORE python ever saw an argument.
        const m = /`([^`]*)`/.exec(ac);
        ok("injection: rendered a backtick-fenced suggested command", m !== null, ac);
        if (m) {
            const suggested = m[1];
            const runDir = path.join(TMP, "hostile-run-cwd");
            fs.mkdirSync(runDir, { recursive: true });
            const br = spawnSync("bash", ["-c", suggested], {
                cwd: runDir, encoding: "utf8", timeout: 15000,
            });
            ok("injection: canary file was NOT created (no command injection)",
                !fs.existsSync(CANARY), "bash stderr: " + (br.stderr || "").slice(0, 300));
        }
    }
}

console.log("\n[13] storeDirectory plugin userConfig feeds ZMEM_DATA (claude host only)");

{
    const custom = path.join(TMP, "custom-store");
    const savedRoot = process.env.CLAUDE_PLUGIN_ROOT;
    const savedOpt = process.env.CLAUDE_PLUGIN_OPTION_STOREDIRECTORY;
    const savedData = process.env.ZMEM_DATA;
    const restore = () => {
        if (savedRoot === undefined) delete process.env.CLAUDE_PLUGIN_ROOT;
        else process.env.CLAUDE_PLUGIN_ROOT = savedRoot;
        if (savedOpt === undefined) delete process.env.CLAUDE_PLUGIN_OPTION_STOREDIRECTORY;
        else process.env.CLAUDE_PLUGIN_OPTION_STOREDIRECTORY = savedOpt;
        if (savedData === undefined) delete process.env.ZMEM_DATA;
        else process.env.ZMEM_DATA = savedData;
    };

    process.env.CLAUDE_PLUGIN_ROOT = REPO;
    delete process.env.ZMEM_DATA;
    process.env.CLAUDE_PLUGIN_OPTION_STOREDIRECTORY = custom;
    eq("storeDirectory: claude + no ZMEM_DATA → plugin option wins",
        launch.buildCanonicalEnv("claude", { cwd: PROJ }, "recall").ZMEM_DATA, custom);

    // Explicit env still beats the plugin option.
    process.env.ZMEM_DATA = path.join(TMP, "explicit-store");
    eq("storeDirectory: explicit ZMEM_DATA still overrides the plugin option",
        launch.buildCanonicalEnv("claude", { cwd: PROJ }, "recall").ZMEM_DATA,
        path.join(TMP, "explicit-store"));
    delete process.env.ZMEM_DATA;

    // zcode never sees the CC-only option.
    eq("storeDirectory: zcode ignores CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
        launch.buildCanonicalEnv("zcode", { cwd: PROJ }, "recall").ZMEM_DATA,
        path.join(os.homedir(), ".zmem"));

    // The manifest default is the LITERAL string "~/.zmem" — CC hands
    // userConfig values through unexpanded, and neither python nor Windows
    // resolves a literal tilde path.
    process.env.CLAUDE_PLUGIN_OPTION_STOREDIRECTORY = "~/.zmem";
    eq("storeDirectory: leading ~ is expanded to the home dir",
        launch.buildCanonicalEnv("claude", { cwd: PROJ }, "recall").ZMEM_DATA,
        path.join(os.homedir(), ".zmem"));

    // Blank/whitespace option falls through to the default rather than
    // producing an empty ZMEM_DATA.
    process.env.CLAUDE_PLUGIN_OPTION_STOREDIRECTORY = "   ";
    eq("storeDirectory: blank option falls through to ~/.zmem",
        launch.buildCanonicalEnv("claude", { cwd: PROJ }, "recall").ZMEM_DATA,
        path.join(os.homedir(), ".zmem"));

    restore();
}

console.log("\n[14] Context budget is measured in ENCODED UTF-8 BYTES, not UTF-16 units");

{
    // 4000 emoji: 8000 UTF-16 code units (under a 9000 budget by .length) but
    // 16000 UTF-8 bytes (well over it). The old `.length` check let this
    // through un-truncated and blew the documented encoded-byte budget.
    const emoji = "🚀".repeat(4000);
    const rawEnv = launch.makeEnvelope("claude", "recall", emoji);
    const rawUnits = JSON.stringify(rawEnv).length;
    const rawBytes = Buffer.byteLength(JSON.stringify(rawEnv), "utf8");
    ok("budget(utf8): fixture is UNDER budget by UTF-16 .length (the old check)",
        rawUnits <= 9000, "utf16units=" + rawUnits);
    ok("budget(utf8): fixture is OVER budget in encoded bytes (the real limit)",
        rawBytes > 9000, "bytes=" + rawBytes);
    const env = launch.fitEnvelope("claude", "recall", emoji, 9000);
    const bytes = Buffer.byteLength(JSON.stringify(env), "utf8");
    ok("budget(utf8): encoded envelope <= 9000 BYTES", bytes <= 9000, "bytes=" + bytes);
    ok("budget(utf8): multi-byte content DID trigger truncation",
        /\[recall truncated\]/.test(env.hookSpecificOutput.additionalContext));
    ok("budget(utf8): no lone surrogate left at the cut",
        !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(env.hookSpecificOutput.additionalContext));

    // Non-Latin script (3 bytes/char, 1 UTF-16 unit) — the case .length
    // under-counts worst.
    const cjk = "記".repeat(4000);
    const cjkEnv = launch.fitEnvelope("claude", "recall", cjk, 9000);
    ok("budget(utf8): CJK content truncated to <= 9000 bytes",
        Buffer.byteLength(JSON.stringify(cjkEnv), "utf8") <= 9000,
        "bytes=" + Buffer.byteLength(JSON.stringify(cjkEnv), "utf8"));

    // ASCII regression: an already-fitting payload is returned untouched.
    const small = launch.fitEnvelope("claude", "recall", "tiny", 9000);
    eq("budget(utf8): fitting payload passes through unchanged",
        small.hookSpecificOutput.additionalContext, "tiny");
}

console.log("\n[15] Recall survives a memory whose CONTENT contains the sentinel");

{
    // A stored memory containing the literal "<<<ZMEM_JSON>>>" used to move the
    // launcher's extraction boundary into the middle of the JSON: the parse
    // failed and the whole recall silently degraded to {} (a self-DoS of that
    // turn's recall — fail-open, not an injection vector). The scripts now
    // neutralize the tokens before wrapping.
    const HDATA = path.join(TMP, "sentineldata");
    fs.mkdirSync(HDATA, { recursive: true });
    const HNS = resolveNs(PROJ);
    seed(HDATA, HNS, "lesson",
        "SENTINEL_CANARY the marker <<<ZMEM_JSON>>> and <<<END>>> appear in this memory verbatim.",
        0.9);

    // main recall path (UserPromptSubmit)
    {
        const r = runLauncher("recall",
            JSON.stringify({ prompt: "tell me about the SENTINEL_CANARY marker memory", cwd: PROJ }),
            envWith({ ZMEM_DATA: HDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("sentinel-in-content/recall: valid JSON envelope (not dropped)",
            obj !== null, r.stdout.slice(0, 300));
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("sentinel-in-content/recall: the canary memory survived the round-trip",
            /SENTINEL_CANARY/.test(ac), ac.slice(0, 300));
        ok("sentinel-in-content/recall: embedded START token was neutralized",
            /ZMEM_JSON_NEUTRALIZED/.test(ac) && !/<<<ZMEM_JSON>>>/.test(ac), ac.slice(0, 300));
    }

    // subagent recall path (SubagentStart) — same wrap pattern, same defense.
    {
        const r = runLauncher("subagent-recall",
            JSON.stringify({ session_id: "sent-sess", cwd: PROJ, agent_id: "a1", agent_type: "coder" }),
            envWith({ ZMEM_DATA: HDATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        ok("sentinel-in-content/subagent-recall: valid JSON envelope (not dropped)",
            obj !== null, r.stdout.slice(0, 300));
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("sentinel-in-content/subagent-recall: the canary memory survived",
            /SENTINEL_CANARY/.test(ac), ac.slice(0, 300));
        ok("sentinel-in-content/subagent-recall: embedded START token was neutralized",
            /ZMEM_JSON_NEUTRALIZED/.test(ac) && !/<<<ZMEM_JSON>>>/.test(ac), ac.slice(0, 300));
    }
}

console.log("\n[16] injection: hostile origin remote must not escape reflect / capture-failure / subagent-reflect suggested commands");

{
    // Same vulnerability class as block [12]'s convention-capture test, applied
    // to the three remaining hooks that render a suggested `store.py add ...`
    // command from a git-remote-derived (repository-controlled) namespace:
    // zmem-reflect.sh (two rendered sites: the no-failures success nudge AND
    // the failures-detected prompt), zmem-capture-failure.sh, and
    // zmem-subagent-reflect.sh. All three drive the REAL hook through the REAL
    // launcher against a REAL git repo with a hostile origin remote.
    function makeHostileRepo(name) {
        const dir = path.join(TMP, name);
        fs.mkdirSync(dir, { recursive: true });
        const canary = path.join(dir, "PWNED_CANARY");
        const hostileRemote = '"; $(touch ' + canary.replace(/\\/g, "/") + '); echo "';
        execFileSync("git", ["init", "-q"], { cwd: dir });
        execFileSync("git", ["remote", "add", "origin", hostileRemote], { cwd: dir });
        const hostileNs = resolveNs(dir);
        ok("injection[" + name + "]: hostile origin survives into the resolved namespace (sanity)",
            hostileNs.indexOf('"') !== -1 && hostileNs.indexOf("$(") !== -1, hostileNs);
        return { dir, canary };
    }

    // Extract the suggested `store.py add ...` command from additionalContext,
    // run it through bash exactly as a copy-pasting agent would, and assert the
    // canary was NOT created. Fixed: shlex.quote() renders the hostile content as
    // inert single-quoted text. Broken (pre-fix): bash expands $(touch ...) while
    // parsing the command line, creating the canary before python ever runs.
    //
    // reflect/subagent-reflect/convention-capture wrap the suggested command in
    // a SINGLE pair of backticks; capture-failure does not backtick-fence it at
    // all (it's just an indented line terminated by a real newline). Handle
    // both shapes rather than assuming backtick-fencing everywhere.
    function extractSuggestedCommand(ac) {
        const backtick = /`([^`]*store\.py[^`]*)`/.exec(ac);
        if (backtick) return backtick[1];
        const line = /^[ \t]*(\S[^\n]*store\.py[^\n]*)$/m.exec(ac);
        if (line) return line[1].replace(/`/g, "");
        return null;
    }
    function runSuggestedAndCheckCanary(label, ac, canary) {
        const cmd = extractSuggestedCommand(ac);
        ok("injection[" + label + "]: rendered a suggested command", cmd !== null, ac.slice(0, 300));
        if (!cmd) return;
        const runDir = fs.mkdtempSync(path.join(TMP, "hostile-run-"));
        const br = spawnSync("bash", ["-c", cmd], { cwd: runDir, encoding: "utf8", timeout: 15000 });
        ok("injection[" + label + "]: canary file was NOT created (no command injection)",
            !fs.existsSync(canary), "bash stderr: " + (br.stderr || "").slice(0, 300));
    }

    // --- reflect: end-to-end, FAILURES branch (the 2nd unquoted site, ~L253) ---
    {
        const { dir: GITPROJ, canary: CANARY } = makeHostileRepo("hostile-reflect-fail");
        const HDATA = path.join(TMP, "hostile-reflect-fail-data");
        fs.mkdirSync(HDATA, { recursive: true });
        seed(HDATA, "user:global", "fact", "seed row to create the store schema.", 0.9);

        const TRANSCRIPT = path.join(TMP, "hostile-reflect-fail.jsonl");
        fs.writeFileSync(TRANSCRIPT, [
            JSON.stringify({ type: "assistant", message: { role: "assistant", content: [
                { type: "tool_use", id: "htu1", name: "Bash", input: { command: "false" } }] } }),
            JSON.stringify({ type: "user", message: { role: "user", content: [
                { type: "tool_result", content: "Exit code 1", is_error: true, tool_use_id: "htu1" }] },
                toolUseResult: "Error: Exit code 1" }),
        ].join("\n") + "\n");

        const r = runLauncher("reflect", JSON.stringify({
            session_id: "reflect-hostile-fail-" + Date.now(), transcript_path: TRANSCRIPT,
            cwd: GITPROJ, hook_event_name: "Stop", stop_hook_active: false,
        }), envWith({ ZMEM_DATA: HDATA, CLAUDE_PROJECT_DIR: GITPROJ, CLAUDE_PLUGIN_ROOT: REPO }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("injection[reflect-fail]: hook still renders the failure reflection prompt",
            /failed tool call/.test(ac), ac.slice(0, 300));
        runSuggestedAndCheckCanary("reflect-fail", ac, CANARY);
    }

    // --- reflect: end-to-end, NO-FAILURES branch (the 1st unquoted site, ~L208) ---
    {
        const { dir: GITPROJ, canary: CANARY } = makeHostileRepo("hostile-reflect-nofail");
        const HDATA = path.join(TMP, "hostile-reflect-nofail-data");
        fs.mkdirSync(HDATA, { recursive: true });
        seed(HDATA, "user:global", "fact", "seed row to create the store schema.", 0.9);

        const TRANSCRIPT = path.join(TMP, "hostile-reflect-nofail.jsonl");
        fs.writeFileSync(TRANSCRIPT, JSON.stringify({ type: "assistant",
            message: { role: "assistant", content: [{ type: "text", text: "done" }] } }) + "\n");

        const r = runLauncher("reflect", JSON.stringify({
            session_id: "reflect-hostile-nofail-" + Date.now(), transcript_path: TRANSCRIPT,
            cwd: GITPROJ, hook_event_name: "Stop", stop_hook_active: false,
        }), envWith({ ZMEM_DATA: HDATA, CLAUDE_PROJECT_DIR: GITPROJ, CLAUDE_PLUGIN_ROOT: REPO }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("injection[reflect-nofail]: hook still renders the success-reflection nudge",
            /ZMem reflection/.test(ac), ac.slice(0, 300));
        runSuggestedAndCheckCanary("reflect-nofail", ac, CANARY);
    }

    // --- capture-failure: end-to-end (PostToolUseFailure) ----------------------
    {
        const { dir: GITPROJ, canary: CANARY } = makeHostileRepo("hostile-capture-failure");
        const HDATA = path.join(TMP, "hostile-capture-failure-data");
        fs.mkdirSync(HDATA, { recursive: true });

        const r = runLauncher("capture-failure", JSON.stringify({
            session_id: "cf-hostile-" + Date.now(), cwd: GITPROJ, tool_name: "Bash",
            tool_input: { command: "false" }, error: "Exit code 1",
        }), envWith({ ZMEM_DATA: HDATA, CLAUDE_PROJECT_DIR: GITPROJ, CLAUDE_PLUGIN_ROOT: REPO }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("injection[capture-failure]: hook still renders the auto-capture prompt",
            /auto-capture/.test(ac), ac.slice(0, 300));
        runSuggestedAndCheckCanary("capture-failure", ac, CANARY);
    }

    // --- subagent-reflect: end-to-end (SubagentStop, subagent's own transcript) ---
    {
        const { dir: GITPROJ, canary: CANARY } = makeHostileRepo("hostile-subagent-reflect");
        const HDATA = path.join(TMP, "hostile-subagent-reflect-data");
        fs.mkdirSync(HDATA, { recursive: true });
        seed(HDATA, "user:global", "fact", "seed row to create the store schema.", 0.9);

        const AGENT_TX = path.join(TMP, "hostile-subagent.jsonl");
        fs.writeFileSync(AGENT_TX, [
            JSON.stringify({ type: "assistant", message: { role: "assistant", content: [
                { type: "tool_use", id: "hatu1", name: "Bash", input: { command: "npm test" } }] } }),
            JSON.stringify({ type: "user", message: { role: "user", content: [
                { type: "tool_result", content: "Exit code 1: tests failed", is_error: true, tool_use_id: "hatu1" }] },
                toolUseResult: "Error: Exit code 1" }),
        ].join("\n") + "\n");
        const PARENT_TX = path.join(TMP, "hostile-subagent-parent.jsonl");
        fs.writeFileSync(PARENT_TX, JSON.stringify({ type: "assistant",
            message: { role: "assistant", content: [{ type: "text", text: "done" }] } }) + "\n");

        const r = runLauncher("subagent-reflect", JSON.stringify({
            session_id: "sr-hostile-" + Date.now(), transcript_path: PARENT_TX, cwd: GITPROJ,
            agent_id: "agentHostile", agent_type: "coder", hook_event_name: "SubagentStop",
            stop_hook_active: false, agent_transcript_path: AGENT_TX,
        }), envWith({ ZMEM_DATA: HDATA, CLAUDE_PROJECT_DIR: GITPROJ, CLAUDE_PLUGIN_ROOT: REPO }));
        let obj = null; try { obj = JSON.parse(r.stdout.trim()); } catch (e) { /* */ }
        const ac = (obj && obj.hookSpecificOutput && obj.hookSpecificOutput.additionalContext) || "";
        ok("injection[subagent-reflect]: hook still renders the subagent reflection prompt",
            /subagent reflection/.test(ac), ac.slice(0, 300));
        runSuggestedAndCheckCanary("subagent-reflect", ac, CANARY);
    }
}

// --- cleanup + report ------------------------------------------------------
try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* */ }

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
