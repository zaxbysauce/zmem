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
//   "command": "node \"${PLUGIN_ROOT}/hooks/zmem-launch.js\" <hook-name>"
//
// Fail-open everywhere: on any error the launcher emits `{}` (for translated
// hooks) or passes the child through, and exits 0 — a memory hiccup never
// blocks a session or a prompt.

"use strict";

const { spawn, execFileSync } = require("child_process");
const { existsSync } = require("fs");
const { join, dirname, basename, delimiter } = require("path");
const { homedir } = require("os");

// Hooks that emit the <<<ZMEM_JSON>>> sentinel and get envelope translation.
// Every OTHER hook is passed through verbatim with its own exit code preserved
// — a hook that has not been migrated to the sentinel would have its real
// output replaced with `{}` if it were translated. Later phases add their names
// here as they adopt the sentinel.
//
// convention-capture (PostToolUse) was previously EXCLUDED here, which made it
// silently non-functional on Claude Code: it emits a bare {additionalContext},
// but CC only honors hookSpecificOutput.additionalContext, so the capture
// prompt was passed through verbatim and never injected. It now emits the
// sentinel (like every other injecting hook) and is translated here.
//
// reflect (Stop) and capture-failure emit the sentinel and carry a bare
// {additionalContext}. On Claude Code the launcher rewraps that to
// hookSpecificOutput.additionalContext, which CC honors on BOTH events
// (confirmed empirically, CC 2.1.218); on Codex it uses the same structured
// envelope but maps capture-failure to PostToolUse because Codex does not
// expose PostToolUseFailure. On ZCode it stays bare. reflect relies on the
// encoded-budget clamp here for its (potentially large, fenced) failure block.
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
    "convention-capture",
]);

// Hooks whose scripts actually read $ZMEM_NAMESPACE. Resolving the namespace
// spawns a python + git subprocess (~100ms cold-start); every OTHER hook gets
// ZMEM_NAMESPACE left EMPTY so that cost is never paid. This set is
// intentionally separate from TRANSLATED_HOOKS above — they answer different
// questions (envelope translation vs. namespace need) and must not be aliased;
// a future hook could need one without the other.
//
// CORRECTNESS OVER PERF (reverses part of the Phase 8 skip): convention-capture
// is back in this set. It fires on every Edit/Write/Bash, so the skip saved
// ~100ms on a hot path — but it used to compute its own basename-derived
// NS_HINT, which suggested storing captured conventions under a namespace the
// unified (git-remote-derived) recall path never queries. Captured conventions
// were therefore invisible to the shared store. Paying the resolution here is
// the correct trade.
const NEEDS_NAMESPACE = new Set([
    "session-start",
    "recall",
    "subagent-recall",
    "reflect",
    "capture-failure",
    "subagent-reflect",
    "convention-capture",
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
// Explicit ZMEM_HOST wins; else Codex's PLUGIN_ROOT/PLUGIN_DATA, then
// Claude/ZCode compatibility vars. Default 'zcode' when neither is present
// (back-compat: the original tool, and the bare-env manual-install / test
// case — a bare additionalContext envelope, no host-specific rewrap).
function detectHost() {
    const explicit = process.env.ZMEM_HOST;
    if (explicit) return explicit;
    if (process.env.PLUGIN_ROOT || process.env.PLUGIN_DATA) return "codex";
    if (process.env.CLAUDE_PLUGIN_ROOT) return "claude";
    if (process.env.ZCODE_PLUGIN_ROOT) return "zcode";
    return "zcode";
}

// --- Resolve plugin root ----------------------------------------------------
function getPluginRoot() {
    return (
        process.env.PLUGIN_ROOT ||
        process.env.CLAUDE_PLUGIN_ROOT ||
        process.env.ZCODE_PLUGIN_ROOT ||
        dirname(__dirname)
    );
}

// --- Find python (for namespace resolution) ---------------------------------
// Windows: prefer `python` (python3 is often a no-op Store stub). Verified by
// actually resolving the namespace; on failure we fall back to 'user:global'.
function resolveNamespace(projectDir) {
    if (!projectDir) return "user:global";
    const scriptsDir = join(getPluginRoot(), "skills", "memory", "scripts");
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

// --- Expand a leading ~ in a config-supplied path ---------------------------
// The plugin manifest's storeDirectory default is the LITERAL string "~/.zmem";
// Claude Code hands userConfig values through unexpanded, and neither python
// nor Windows resolves a literal "~" path. Blank/whitespace-only → "" (falls
// through to the next precedence source).
function expandHome(p) {
    const s = (p || "").trim();
    if (!s) return "";
    if (s === "~") return homedir();
    if (s.startsWith("~/") || s.startsWith("~\\")) return join(homedir(), s.slice(2));
    return s;
}

function hookEventNameFor(host, hookName) {
    if (host === "codex" && hookName === "capture-failure") return "PostToolUse";
    return EVENT_MAP[hookName] || hookName;
}

function isFailureStatus(status) {
    const normalized = String(status || "").trim().toLowerCase();
    if (!normalized) return false;
    return !["ok", "success", "succeeded", "completed", "complete"].includes(normalized);
}

function firstNonEmpty(...values) {
    for (const value of values) {
        if (typeof value === "string" && value.trim()) return value;
    }
    return "";
}

function normalizeErrorValue(value) {
    if (typeof value === "string") {
        const trimmed = value.trim();
        return trimmed ? trimmed : null;
    }
    if (value && typeof value === "object") {
        const message = typeof value.message === "string" ? value.message.trim() : "";
        const type = typeof value.type === "string" ? value.type.trim() : "";
        if (!message && !type) return null;
        return {
            ...value,
            ...(message ? { message } : {}),
            ...(type ? { type } : {}),
        };
    }
    return null;
}

// Codex failure capture runs on PostToolUse because there is no dedicated
// PostToolUseFailure event. Normalize the stable PostToolUse payload into the
// shape the existing capture-failure hook script already understands. If the
// payload does not clearly describe a failure, return null and fail open.
function normalizeCodexFailurePayload(meta) {
    if (!meta || typeof meta !== "object") return null;

    const status = firstNonEmpty(
        meta.status,
        meta.tool_status,
        meta.toolStatus,
        meta.result && meta.result.status,
        meta.tool_result && meta.tool_result.status
    );
    const failed = isFailureStatus(status);

    const error = normalizeErrorValue(
        meta.error ||
            meta.tool_error ||
            meta.toolError ||
            (meta.result && meta.result.error) ||
            (meta.tool_result && meta.tool_result.error) ||
            (meta.tool_output && meta.tool_output.error) ||
            (failed &&
                firstNonEmpty(
                    meta.stderr,
                    meta.message,
                    meta.failure,
                    meta.tool_message,
                    meta.toolMessage,
                    meta.result && meta.result.message,
                    meta.tool_result && meta.tool_result.message
                ))
    );

    if (!failed && !error) return null;
    if (!error) return null;

    return {
        ...meta,
        session_id: firstNonEmpty(meta.session_id, meta.sessionId),
        tool_name: firstNonEmpty(
            meta.tool_name,
            meta.toolName,
            meta.tool && meta.tool.name,
            meta.name
        ),
        tool_input:
            meta.tool_input ||
            meta.toolInput ||
            (meta.tool && meta.tool.input) ||
            meta.arguments ||
            {},
        error,
    };
}

function prepareHookPayload(host, hookName, stdinBuf, meta) {
    if (host !== "codex" || hookName !== "capture-failure") {
        return { input: stdinBuf, meta };
    }
    const normalized = normalizeCodexFailurePayload(meta);
    if (!normalized) return null;
    return {
        input: Buffer.from(JSON.stringify(normalized), "utf8"),
        meta: normalized,
    };
}

// --- Build the canonical ZMEM_* env for the child ---------------------------
// hookName is optional (back-compat for direct callers/tests that don't care
// about the namespace-skip): omitted/unrecognized names get the namespace
// resolved (safe default — never SILENTLY skip for a hook that needs it).
function buildCanonicalEnv(host, meta, hookName) {
    const env = { ...process.env };

    const project =
        process.env.CODEX_PROJECT_DIR ||
        process.env.CLAUDE_PROJECT_DIR ||
        process.env.ZCODE_PROJECT_DIR ||
        (meta && meta.cwd) ||
        "";
    const session =
        process.env.CLAUDE_SESSION_ID ||
        (meta && (meta.session_id || meta.sessionId)) ||
        "";
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

    // ZMEM_DATA precedence:
    //   1. explicit ZMEM_DATA env (an operator override always wins)
    //   2. the plugin userConfig `storeDirectory` option — Claude Code exports
    //      each userConfig key to the hook process as
    //      CLAUDE_PLUGIN_OPTION_<KEY-UPPERCASED>. Only consulted on the claude
    //      host (it never exists on zcode). Without this the manifest option
    //      was declared but had NO runtime effect.
    //   3. the box-wide default ~/.zmem.
    //
    // PLUGIN_DATA is deliberately NOT a store candidate. Codex owns that
    // directory as per-install plugin state; using it here would silently
    // split Codex away from the Claude/ZCode box-wide store.
    // Exporting this is the cutover wiring — store.py resolves
    // <ZMEM_DATA>/store.sqlite ahead of the legacy per-plugin data dirs.
    const pluginOptData =
        host === "claude" ? expandHome(process.env.CLAUDE_PLUGIN_OPTION_STOREDIRECTORY) : "";
    const zmemData = process.env.ZMEM_DATA || pluginOptData || join(homedir(), ".zmem");

    // ZMEM_SKILLS_DIRS: existing env wins (mirrors ZMEM_DATA above); else the
    // box-wide default of BOTH skills dirs, delimiter-joined the same way
    // host.py's resolve_skills_dirs() parses it (os.pathsep — ';' on win32,
    // ':' elsewhere), so hook-context and skill-context (store.py invoked
    // directly by a skill/agent) always agree on the same target set.
    // Promotion writes to every dir here regardless of which host promoted —
    // a lesson promoted from either tool becomes a skill visible to both.
    const defaultSkillsDirs =
        host === "codex"
            ? [
                  join(homedir(), ".codex", "skills"),
                  join(homedir(), ".claude", "skills"),
                  join(homedir(), ".zcode", "skills"),
              ]
            : [join(homedir(), ".claude", "skills"), join(homedir(), ".zcode", "skills")];
    const skillsDirs = process.env.ZMEM_SKILLS_DIRS || defaultSkillsDirs.join(delimiter);
    const tier0 = host === "zcode" ? "zmem" : "native";
    // Respect an operator-set ZMEM_CTX_BUDGET so resolveBudget() can actually
    // observe, validate, and clamp it (#39 E3 / PRR-001). The former
    // unconditional host default overwrote the env var before resolveBudget
    // ran, making the validation unreachable and the knob dead end-to-end.
    // Falls back to the host default when unset, matching the skillsDirs/zmemData
    // pattern above.
    const ctxBudget = process.env.ZMEM_CTX_BUDGET || (host === "zcode" ? "25000" : "9000");

    env.ZMEM_HOST = host;
    env.ZMEM_ROOT = getPluginRoot();
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

function buildChildEnv(env, bashPath) {
    const childEnv = { ...env };
    if (process.platform !== "win32" || !bashPath || !existsSync(bashPath)) return childEnv;

    const bashDir = dirname(bashPath);
    const parentDir = dirname(bashDir);
    const gitRoot = basename(bashDir).toLowerCase() === "bin" &&
        basename(parentDir).toLowerCase() === "usr"
        ? dirname(parentDir)
        : parentDir;
    const extraDirs = [
        bashDir,
        join(gitRoot, "usr", "bin"),
        join(gitRoot, "bin"),
    ].filter((dir, index, items) => existsSync(dir) && items.indexOf(dir) === index);

    if (extraDirs.length === 0) return childEnv;

    const currentPath = childEnv.PATH || childEnv.Path || "";
    const mergedPath = extraDirs.concat(currentPath ? [currentPath] : []).join(delimiter);
    childEnv.PATH = mergedPath;
    childEnv.Path = mergedPath;
    return childEnv;
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
    if (host === "claude" || host === "codex") {
        return {
            hookSpecificOutput: {
                hookEventName: hookEventNameFor(host, hookName),
                additionalContext: content,
            },
        };
    }
    return { additionalContext: content };
}

// Encoded size of an envelope in UTF-8 BYTES. `String.prototype.length` counts
// UTF-16 code units, which UNDER-counts every non-ASCII character (an emoji is
// 2 UTF-16 units but 4 UTF-8 bytes, CJK is 1 unit but 3 bytes). The budget is
// documented and enforced downstream in encoded bytes, so measuring with
// .length let Unicode-heavy recalls blow past it.
function encodedSize(value) {
    return Buffer.byteLength(JSON.stringify(value), "utf8");
}

// content.slice() cuts on UTF-16 code units, so a cut inside a surrogate pair
// leaves a lone high surrogate (which JSON.stringify escapes as a 6-byte
// "\udXXX", making byte length non-monotonic in the cut index). Drop a trailing
// lone high surrogate so the binary search stays monotonic and no truncated
// envelope ever carries a broken code point.
function sliceSafe(s, n) {
    if (n <= 0) return "";
    if (n >= s.length) return s;
    const last = s.charCodeAt(n - 1);
    if (last >= 0xd800 && last <= 0xdbff) return s.slice(0, n - 1);
    return s.slice(0, n);
}

// Enforce budget on the ENCODED envelope (JSON escaping of newline/quote-dense
// blocks inflates length, so raw-content length is not enough). If over, binary
// search the largest content prefix whose encoded envelope + truncation marker
// fits within budget.
function fitEnvelope(host, hookName, content, budget) {
    let env = makeEnvelope(host, hookName, content);
    if (encodedSize(env) <= budget) return env;

    const marker = "\n[recall truncated]";
    let lo = 0;
    let hi = content.length;
    let best = null;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const cand = sliceSafe(content, mid) + marker;
        const e = makeEnvelope(host, hookName, cand);
        if (encodedSize(e) <= budget) {
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
    return encodedSize(minimal) <= budget ? minimal : {};
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

// Resolve and VALIDATE the context budget (issue #39 E3). A negative value is
// truthy after parseInt (e.g. parseInt("-5") === -5), so the former
// `parseInt(env.ZMEM_CTX_BUDGET, 10) || 9000` let it through: fitEnvelope then
// saw `encodedSize(env) <= -5` as always false and returned {} — silently
// injecting zero memory with no error anywhere. Non-numeric and zero are
// already safe (NaN/0 are falsy → 9000), but this helper handles all invalid
// shapes uniformly with a clear stderr warning, and clamps an absurdly large
// value rather than letting the envelope swallow the whole context window.
const BUDGET_DEFAULT = 9000;
const BUDGET_MAX = 1000000; // ~15 max-size (65536-char) memories in one envelope
function resolveBudget(env, stderrWriter) {
    const raw = env && env.ZMEM_CTX_BUDGET;
    const parsed = parseInt(raw, 10);
    const warn = typeof stderrWriter === "function" ? stderrWriter
        : (s) => process.stderr.write(s);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        warn(`zmem: invalid ZMEM_CTX_BUDGET=${JSON.stringify(raw)} `
             + `(must be a positive integer); using default ${BUDGET_DEFAULT}\n`);
        return BUDGET_DEFAULT;
    }
    if (parsed > BUDGET_MAX) {
        warn(`zmem: ZMEM_CTX_BUDGET=${parsed} exceeds sane max ${BUDGET_MAX}; clamping\n`);
        return BUDGET_MAX;
    }
    return parsed;
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
    const scriptPath = join(getPluginRoot(), "hooks", `zmem-${hookName}.sh`);

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
    const prepared = prepareHookPayload(host, hookName, stdinBuf, meta);
    if (!prepared) {
        process.stdout.write("{}\n");
        process.exit(0);
        return;
    }

    const env = buildCanonicalEnv(host, prepared.meta, hookName);
    const budget = resolveBudget(env);
    // Export the validated/clamped budget so spawned hook scripts see the same
    // effective value the launcher uses internally (#39 E3 / cubic-re #1).
    // Without this, a huge operator-set ZMEM_CTX_BUDGET is clamped for
    // fitEnvelope but propagated unclamped to child shell scripts that read
    // $ZMEM_CTX_BUDGET directly.
    env.ZMEM_CTX_BUDGET = String(budget);
    const translated = TRANSLATED_HOOKS.has(hookName);
    const bashPath = findBash();

    // Translated hooks: buffer child stdout so we can rewrap it. Pass-through
    // hooks: inherit stdout so their output reaches the runner unchanged.
    const child = spawn(bashPath, [scriptPath], {
        stdio: ["pipe", translated ? "pipe" : "inherit", "inherit"],
        env: buildChildEnv(env, bashPath),
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
        child.stdin.write(prepared.input);
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
    getPluginRoot,
    resolveNamespace,
    buildCanonicalEnv,
    buildChildEnv,
    hookEventNameFor,
    normalizeCodexFailurePayload,
    prepareHookPayload,
    extractPayload,
    makeEnvelope,
    fitEnvelope,
    translate,
    resolveBudget,
    EVENT_MAP,
    TRANSLATED_HOOKS,
    NEEDS_NAMESPACE,
};
