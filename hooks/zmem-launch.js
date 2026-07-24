#!/usr/bin/env node
// zmem-launch.js — Cross-platform hook launcher + host adapter for ZMem.
//
// TWO JOBS:
//
// 1. Shell resolution (original job): ZCode/Claude Code hook runners resolve the
//    shell for `type: "command"` hooks via cmd.exe on Windows, which finds WSL's
//    bash.exe before Git Bash. WSL bash cannot run the hook scripts. This Node
//    launcher is invoked instead of bare `bash` — Node is a real .exe on PATH
//    (no shell-resolution ambiguity) — finds the correct Git Bash, and spawns
//    the target hook script under it.
//
// 2. Host adapter (Phase 3): the launcher owns ALL host knowledge so the bash
//    scripts and store.py never branch on host. It:
//      - detects the host (claude | zcode),
//      - reads stdin once and replays the EXACT bytes to the child (the child
//        scripts still need the full hook payload),
//      - exports a canonical ZMEM_* env (see buildCanonicalEnv),
//      - translates the child's stdout envelope for the sentinel-emitting hooks
//        (session-start, recall): extract the payload from a
//        <<<ZMEM_JSON>>>…<<<END>>> sentinel, rewrap {additionalContext} into the
//        host-appropriate shape, and enforce an ENCODED context budget.
//
// Usage in hooks.json:
//   "command": "node \"${ZCODE_PLUGIN_ROOT}/hooks/zmem-launch.js\" <hook-name>"
//
// Fail-open everywhere: on any error the launcher emits `{}` (for translated
// hooks) or passes the child through, and exits 0 — a memory hiccup never
// blocks a session or a prompt.

"use strict";

const { spawn, execFileSync } = require("child_process");
const { existsSync } = require("fs");
const { join, dirname } = require("path");
const { homedir } = require("os");

// Hooks that emit the <<<ZMEM_JSON>>> sentinel and get envelope translation.
// Every OTHER hook (convention-capture, …) is passed through verbatim with its
// own exit code preserved — it has not been migrated to the sentinel yet, so
// translating it would replace its real output with `{}`. Later phases add
// their names here as they adopt the sentinel.
//
// reflect (Stop) and capture-failure (PostToolUseFailure) emit the sentinel and
// carry a bare {additionalContext}. On Claude Code the launcher rewraps that to
// hookSpecificOutput.additionalContext, which CC honors on BOTH events
// (confirmed empirically, CC 2.1.218); on ZCode it stays bare. reflect relies on
// the encoded-budget clamp here for its (potentially large, fenced) failure
// block.
// subagent-recall (SubagentStart) and subagent-reflect (SubagentStop) both
// inject additionalContext. Empirically confirmed (CC 2.1.218): CC honors
// hookSpecificOutput.additionalContext on BOTH events — SubagentStart injects
// into the fresh subagent's context (cannot block startup); SubagentStop
// re-loops the subagent turn (flipping stop_hook_active, which the reflect loop
// guard keys on). Both therefore get envelope translation + the encoded budget.
const TRANSLATED_HOOKS = new Set([
    "session-start",
    "recall",
    "reflect",
    "capture-failure",
    "subagent-recall",
    "subagent-reflect",
]);

// Hooks whose scripts actually read $ZMEM_NAMESPACE (verified via
// `grep -l ZMEM_NAMESPACE hooks/*.sh` — see PLAN.md Phase 8). Resolving the
// namespace spawns a python + git subprocess (~100ms cold-start); every OTHER
// hook (today: only convention-capture, which fires on every Edit/Write/Bash
// and computes its own basename-derived NS_HINT instead) gets ZMEM_NAMESPACE
// left EMPTY so that cost is never paid on the hot per-edit path. This set is
// intentionally separate from TRANSLATED_HOOKS above — same membership today
// by coincidence (both happen to be the sentinel-emitting hooks), but they
// answer different questions (envelope translation vs. namespace need) and
// must not be aliased; a future hook could need one without the other.
const NEEDS_NAMESPACE = new Set([
    "session-start",
    "recall",
    "subagent-recall",
    "reflect",
    "capture-failure",
    "subagent-reflect",
]);

// Hook-name → Claude Code hookEventName (for the {hookSpecificOutput} rewrap).
const EVENT_MAP = {
    "session-start": "SessionStart",
    "recall": "UserPromptSubmit",
    "subagent-recall": "SubagentStart",
    "reflect": "Stop",
    "subagent-reflect": "SubagentStop",
    "capture-failure": "PostToolUseFailure",
    "convention-capture": "PostToolUse",
};

// --- Detect host ------------------------------------------------------------
// Explicit ZMEM_HOST wins; else CLAUDE_PLUGIN_ROOT → claude, ZCODE_PLUGIN_ROOT
// → zcode. Default 'zcode' when neither is present (back-compat: the original
// tool, and the bare-env manual-install / test case — a bare additionalContext
// envelope, no Claude rewrap).
function detectHost() {
    const explicit = process.env.ZMEM_HOST;
    if (explicit) return explicit;
    if (process.env.CLAUDE_PLUGIN_ROOT) return "claude";
    if (process.env.ZCODE_PLUGIN_ROOT) return "zcode";
    return "zcode";
}

// --- Resolve plugin root ----------------------------------------------------
const pluginRoot =
    process.env.CLAUDE_PLUGIN_ROOT ||
    process.env.ZCODE_PLUGIN_ROOT ||
    dirname(__dirname);

// --- Find python (for namespace resolution) ---------------------------------
// Windows: prefer `python` (python3 is often a no-op Store stub). Verified by
// actually resolving the namespace; on failure we fall back to 'user:global'.
function resolveNamespace(projectDir) {
    if (!projectDir) return "user:global";
    const scriptsDir = join(pluginRoot, "skills", "memory", "scripts");
    const code =
        "import sys; sys.path.insert(0, sys.argv[1]); import host; " +
        "print(host.resolve_namespace(sys.argv[2]))";
    const candidates =
        process.platform === "win32" ? ["python", "python3"] : ["python3", "python"];
    for (const py of candidates) {
        try {
            const out = execFileSync(py, ["-c", code, scriptsDir, projectDir], {
                encoding: "utf8",
                timeout: 8000,
                stdio: ["ignore", "pipe", "ignore"],
            }).trim();
            if (out) return out;
        } catch {
            // try next interpreter
        }
    }
    return "user:global";
}

// --- Build the canonical ZMEM_* env for the child ---------------------------
// hookName is optional (back-compat for direct callers/tests that don't care
// about the namespace-skip): omitted/unrecognized names get the namespace
// resolved (safe default — never SILENTLY skip for a hook that needs it).
function buildCanonicalEnv(host, meta, hookName) {
    const env = { ...process.env };

    const project =
        process.env.CLAUDE_PROJECT_DIR ||
        process.env.ZCODE_PROJECT_DIR ||
        (meta && meta.cwd) ||
        "";
    const session =
        process.env.CLAUDE_SESSION_ID || (meta && meta.session_id) || "";
    const transcript = (meta && meta.transcript_path) || "";
    const agentType = (meta && meta.agent_type) || "";
    // SubagentStop carries the SUBAGENT's own transcript separately in
    // agent_transcript_path (…/subagents/agent-<id>.jsonl). The top-level
    // transcript_path on that event is the PARENT session's transcript, which
    // does NOT contain the subagent's internal failed tool calls (the subagent
    // shows up there as one opaque Task result). Failure detection for a
    // subagent must scan agent_transcript_path — confirmed empirically CC
    // 2.1.218 (Phase 7 discovery). agent_id disambiguates sibling subagents that
    // share one session_id, so lesson-dedup can be per-subagent, not per-session.
    const agentTranscript = (meta && meta.agent_transcript_path) || "";
    const agentId = (meta && meta.agent_id) || "";

    // ZMEM_DATA: existing env / userConfig wins; else the box-wide default
    // ~/.zmem (== C:\Users\Brett\.zmem on this box). Exporting this is the
    // cutover wiring — store.py resolves <ZMEM_DATA>/store.sqlite ahead of the
    // legacy per-plugin data dirs.
    const zmemData = process.env.ZMEM_DATA || join(homedir(), ".zmem");

    const skillsDirs =
        host === "claude"
            ? join(homedir(), ".claude", "skills")
            : join(homedir(), ".zcode", "skills");
    const tier0 = host === "claude" ? "native" : "zmem";
    const ctxBudget = host === "claude" ? "9000" : "25000";

    env.ZMEM_HOST = host;
    env.ZMEM_ROOT = pluginRoot;
    env.ZMEM_DATA = zmemData;
    env.ZMEM_PROJECT = project;
    env.ZMEM_SESSION = session;
    env.ZMEM_TRANSCRIPT = transcript;
    env.ZMEM_AGENT_TRANSCRIPT = agentTranscript;
    env.ZMEM_AGENT_TYPE = agentType;
    env.ZMEM_AGENT_ID = agentId;
    // PERF (Phase 8): only resolve the namespace (python + git subprocess,
    // ~100ms cold-start) for hooks that actually consume ZMEM_NAMESPACE. An
    // unrecognized/omitted hookName resolves anyway (fail safe toward
    // correctness, not silently toward speed).
    env.ZMEM_NAMESPACE =
        !hookName || NEEDS_NAMESPACE.has(hookName) ? resolveNamespace(project) : "";
    env.ZMEM_SKILLS_DIRS = skillsDirs;
    env.ZMEM_TIER0 = tier0;
    env.ZMEM_CTX_BUDGET = ctxBudget;

    return env;
}

// --- Find bash --------------------------------------------------------------
// Priority: explicit env > Git Bash at known locations > derive from git > bare 'bash'
function findBash() {
    const envBash = process.env.ZMEM_BASH_PATH;
    if (envBash && existsSync(envBash)) return envBash;

    if (process.platform !== "win32") return "bash";

    const candidates = [
        "C:\\Program Files\\Git\\usr\\bin\\bash.exe",
        "C:\\Program Files\\Git\\bin\\bash.exe",
        join(homedir(), "AppData", "Local", "Programs", "Git", "usr", "bin", "bash.exe"),
        join(homedir(), "AppData", "Local", "Programs", "Git", "bin", "bash.exe"),
        "C:\\Program Files (x86)\\Git\\usr\\bin\\bash.exe",
        "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
    ];
    for (const c of candidates) {
        if (existsSync(c)) return c;
    }

    try {
        const gitPath = execFileSync("where", ["git"], { encoding: "utf8", timeout: 3000 })
            .trim()
            .split("\n")[0]
            .trim();
        if (gitPath && existsSync(gitPath)) {
            const gitDir = dirname(gitPath);
            const maybeRoot = dirname(gitDir);
            const roots = [maybeRoot, dirname(maybeRoot)];
            for (const root of roots) {
                for (const sub of ["usr\\bin\\bash.exe", "bin\\bash.exe"]) {
                    const bashCandidate = join(root, sub);
                    if (existsSync(bashCandidate)) return bashCandidate;
                }
            }
        }
    } catch {
        // git not found — continue to fallback
    }

    return "bash";
}

// --- Sentinel payload extraction --------------------------------------------
// Scripts wrap their JSON as <<<ZMEM_JSON>>>{...}<<<END>>>. Extract the JSON of
// the LAST complete pair (anchor on the last END, then the START that precedes
// it — survives a trailing unterminated START and any stray stdout noise
// bracketing the sentinel). Returns the parsed object, or null on any failure.
function extractPayload(raw) {
    const START = "<<<ZMEM_JSON>>>";
    const END = "<<<END>>>";
    const endIdx = raw.lastIndexOf(END);
    if (endIdx === -1) return null;
    const startIdx = raw.lastIndexOf(START, endIdx);
    if (startIdx === -1) return null;
    const jsonStr = raw.slice(startIdx + START.length, endIdx);
    try {
        return JSON.parse(jsonStr);
    } catch {
        return null;
    }
}

// Wrap additionalContext content into the host-appropriate envelope shape.
function makeEnvelope(host, hookName, content) {
    if (host === "claude") {
        return {
            hookSpecificOutput: {
                hookEventName: EVENT_MAP[hookName] || hookName,
                additionalContext: content,
            },
        };
    }
    return { additionalContext: content };
}

// Enforce budget on the ENCODED envelope (JSON escaping of newline/quote-dense
// blocks inflates length, so raw-content length is not enough). If over, binary
// search the largest content prefix whose encoded envelope + truncation marker
// fits within budget.
function fitEnvelope(host, hookName, content, budget) {
    let env = makeEnvelope(host, hookName, content);
    if (JSON.stringify(env).length <= budget) return env;

    const marker = "\n[recall truncated]";
    let lo = 0;
    let hi = content.length;
    let best = null;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const cand = content.slice(0, mid) + marker;
        const e = makeEnvelope(host, hookName, cand);
        if (JSON.stringify(e).length <= budget) {
            best = e;
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    if (best) return best;
    // Even an empty-prefix + marker overflows (pathologically tiny budget):
    // emit the marker alone; if that still overflows there is nothing sane to
    // trim to, so fall back to {} (fail-open, never inject a broken payload).
    const minimal = makeEnvelope(host, hookName, marker.trim());
    return JSON.stringify(minimal).length <= budget ? minimal : {};
}

// Translate the buffered child stdout into the final envelope for a
// sentinel-emitting hook. Never throws — returns {} on any failure.
function translate(raw, host, hookName, budget) {
    const payload = extractPayload(raw);
    if (payload === null) return {}; // missing/invalid sentinel → fail open
    const content = payload.additionalContext;
    if (content === undefined || content === null || content === "") return {};
    return fitEnvelope(host, hookName, String(content), budget);
}

// --- Read all of stdin (buffered, for parse + verbatim replay) --------------
function readStdin() {
    return new Promise((resolve) => {
        if (process.stdin.isTTY) {
            resolve(Buffer.alloc(0));
            return;
        }
        const chunks = [];
        process.stdin.on("data", (c) => chunks.push(c));
        process.stdin.on("end", () => resolve(Buffer.concat(chunks)));
        process.stdin.on("error", () => resolve(Buffer.concat(chunks)));
    });
}

// --- Main -------------------------------------------------------------------
async function main() {
    const hookName = process.argv[2];
    if (!hookName) {
        // No hook name — can't proceed. Fail open (empty JSON, exit 0).
        process.stdout.write("{}\n");
        process.exit(0);
        return;
    }
    const scriptPath = join(pluginRoot, "hooks", `zmem-${hookName}.sh`);

    if (!existsSync(scriptPath)) {
        // Target script missing — fail open.
        process.stdout.write("{}\n");
        process.exit(0);
        return;
    }

    const stdinBuf = await readStdin();

    // Parse a COPY of stdin to extract fields (tolerate missing / non-JSON).
    let meta = {};
    try {
        meta = JSON.parse(stdinBuf.toString("utf8")) || {};
    } catch {
        meta = {};
    }

    const host = detectHost();
    const env = buildCanonicalEnv(host, meta, hookName);
    const budget = parseInt(env.ZMEM_CTX_BUDGET, 10) || 9000;
    const translated = TRANSLATED_HOOKS.has(hookName);
    const bashPath = findBash();

    // Translated hooks: buffer child stdout so we can rewrap it. Pass-through
    // hooks: inherit stdout so their output reaches the runner unchanged.
    const child = spawn(bashPath, [scriptPath], {
        stdio: ["pipe", translated ? "pipe" : "inherit", "inherit"],
        env,
    });

    child.on("error", () => {
        // Spawn failed (bash not found, etc.) — fail open.
        process.stdout.write("{}\n");
        process.exit(0);
    });

    // Replay the exact original stdin bytes to the child, then close its stdin.
    // Guard EPIPE/ECONNRESET: session-start never reads stdin, so end() can hit
    // an already-closed pipe — swallow it rather than crash the launcher.
    if (child.stdin) {
        child.stdin.on("error", () => {});
        child.stdin.write(stdinBuf);
        child.stdin.end();
    }

    if (translated && child.stdout) {
        const outChunks = [];
        child.stdout.on("data", (c) => outChunks.push(c));
        child.on("close", (code) => {
            const raw = Buffer.concat(outChunks).toString("utf8");
            let envelope;
            try {
                envelope = translate(raw, host, hookName, budget);
            } catch {
                envelope = {};
            }
            process.stdout.write(JSON.stringify(envelope) + "\n");
            // Translated hooks are always fail-open: exit 0 regardless of child.
            process.exit(0);
        });
    } else {
        // Pass-through: preserve the child's exit code (today's behavior).
        child.on("close", (code) => {
            process.exit(code || 0);
        });
    }
}

// Run only when invoked directly; when required as a module (tests) just export
// the pure helpers.
if (require.main === module) {
    main();
}

module.exports = {
    detectHost,
    resolveNamespace,
    buildCanonicalEnv,
    extractPayload,
    makeEnvelope,
    fitEnvelope,
    translate,
    EVENT_MAP,
    TRANSLATED_HOOKS,
    NEEDS_NAMESPACE,
};
