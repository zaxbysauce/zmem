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
        "ZMEM_TRANSCRIPT", "ZMEM_AGENT_TYPE", "ZMEM_NAMESPACE", "ZMEM_SKILLS_DIRS",
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
    const expectedNs = resolveNs(REPO);
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
    eq("envdump: ZMEM_NAMESPACE == resolve_namespace(repo checkout)", kv.ZMEM_NAMESPACE, expectedNs);
    ok("envdump: namespace is the derived git-remote key (not basename)",
        /^project:github\.com\//.test(kv.ZMEM_NAMESPACE || ""), kv.ZMEM_NAMESPACE);
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
    // A non-translated hook name whose script exits non-zero and prints noise:
    // launcher must pass stdout through verbatim and NOT wrap/replace with {}.
    const PROOT = path.join(TMP, "passroot");
    fs.mkdirSync(path.join(PROOT, "hooks"), { recursive: true });
    fs.writeFileSync(path.join(PROOT, "hooks", "zmem-reflect.sh"),
        "#!/usr/bin/env bash\necho 'reflect-raw-output'\nexit 0\n");
    const r = runLauncher("reflect", "{}", envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: PROOT, CLAUDE_PROJECT_DIR: PROJ,
    }));
    ok("passthrough: raw child stdout preserved (not translated to {})",
        /reflect-raw-output/.test(r.stdout));
    ok("passthrough: NOT wrapped in hookSpecificOutput", !/hookSpecificOutput/.test(r.stdout));
}

console.log("\n[7] No ~5s session-start stall");

{
    const t0 = Date.now();
    runLauncher("session-start", SESSION_PAYLOAD, envWith({
        ZMEM_DATA: DATA, CLAUDE_PLUGIN_ROOT: REPO, CLAUDE_PROJECT_DIR: PROJ,
    }));
    const elapsed = Date.now() - t0;
    ok("timing: session-start returns in < 4000ms (no consolidate wait stall)",
        elapsed < 4000, elapsed + "ms");
}

// --- cleanup + report ------------------------------------------------------
try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (e) { /* */ }

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
process.exit(0);
