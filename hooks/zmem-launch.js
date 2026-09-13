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
const { existsSync, mkdirSync, appendFileSync, readFileSync } = require("fs");
const { join, dirname, basename, resolve, delimiter } = require("path");
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
    "pretool-recall",
    "posttoolbatch-recall",
    "reflect",
    "capture-failure",
    "subagent-recall",
    "subagent-reflect",
    "convention-capture",
    "capture-correction",
    "precompact",
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
    "pretool-recall",
    "posttoolbatch-recall",
    "subagent-recall",
    "reflect",
    "capture-failure",
    "subagent-reflect",
    "convention-capture",
    "capture-correction",
    "precompact",
]);

// Hook-name → Claude Code hookEventName (for the {hookSpecificOutput} rewrap).
const EVENT_MAP = {
    "session-start": "SessionStart",
    "recall": "UserPromptSubmit",
    "subagent-recall": "SubagentStart",
    "pretool-recall": "PreToolUse",
    "reflect": "Stop",
    "subagent-reflect": "SubagentStop",
    "capture-failure": "PostToolUseFailure",
    "convention-capture": "PostToolUse",
    "capture-correction": "UserPromptSubmit",
    "precompact": "PreCompact",
    // Issue #118 (D-2 scope 2): Claude-only stash hook — no envelope, no
    // namespace, so it is deliberately absent from TRANSLATED_HOOKS and
    // NEEDS_NAMESPACE (pass-through: the wrapper's `{}` + exit 0 go to the
    // host verbatim).
    "postcompact": "PostCompact",
    // Issue #120 (D-5): Claude-only post-edit batch checkpoint recall. The
    // launcher only TRANSLATES the verb — it never synthesizes the event:
    // only hooks.claude.json registers PostToolBatch (hooks.codex.json has
    // no such upstream event and deliberately stays without one), so a
    // manifest without the entry can never fire this route.
    "posttoolbatch-recall": "PostToolBatch",
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

// --- Timeout budget (issue #121) ---------------------------------------------
// Canonical integer values live in hooks/timeout-budget.json; the runtime
// reads env overrides and falls back to those defaults. The Hermes rows are
// #160-owned documentation inputs — no Hermes deadline is enforced here.
const DEFAULT_LAUNCHER_WATCHDOG_MS = 12000;
const DEFAULT_NAMESPACE_RESOLVE_MS = 2000;
const DEFAULT_NAMESPACE_CACHE_TTL_MS = 60000;
const NAMESPACE_CACHE_MAX_ENTRIES = 128;

// Read one positive-integer millisecond env override. Invalid (non-integer,
// zero, negative) values fall back to the default and write exactly ONE
// warning per name per process (warn is injectable for tests).
function readPositiveIntMs(env, name, dflt, warn) {
    const raw = env && env[name];
    if (raw === undefined || raw === "") return dflt;
    const w = typeof warn === "function" ? warn : (s) => process.stderr.write(s);
    // PR #198 review F-004: parseInt accepted numeric prefixes, so
    // ZMEM_LAUNCHER_WATCHDOG_MS=1e9 silently parsed as 1 ms. Require a full
    // decimal match; anything else takes the documented default + one
    // warning per name. ("15" stays valid — it IS a positive integer; the
    // issue contract defines no minimum.)
    if (!/^[0-9]+$/.test(String(raw).trim())) {
        warnOnce(warn, "env:" + name,
            `zmem: invalid ${name}=${JSON.stringify(raw)} (must be a positive integer); using default ${dflt}\n`);
        return dflt;
    }
    const parsed = parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        warnOnce(warn, "env:" + name,
            `zmem: invalid ${name}=${JSON.stringify(raw)} (must be a positive integer); using default ${dflt}\n`);
        return dflt;
    }
    return parsed;
}

// F-010: hooks/timeout-budget.json is the canonical table — load it at
// startup (fail-open to the hardcoded fallbacks) so the runtime defaults
// and the documented table cannot drift apart.
function loadTimeoutBudget() {
    try {
        return JSON.parse(readFileSync(join(__dirname, "timeout-budget.json"), "utf8"));
    } catch {
        return null;
    }
}
const TIMEOUT_BUDGET = loadTimeoutBudget();
function budgetDefault(key, fallback) {
    const v = TIMEOUT_BUDGET && TIMEOUT_BUDGET[key];
    return (typeof v === "number" && v > 0) ? v : fallback;
}

const _msWarnings = new Set();
function warnOnce(warn, key, message) {
    if (_msWarnings.has(key)) return;
    _msWarnings.add(key);
    const w = typeof warn === "function" ? warn : (s) => process.stderr.write(s);
    try { w(message); } catch { /* fail open */ }
}

// --- Namespace resolution with a process-local cache (issue #121) ------------
// Keyed by the normalized absolute project path (case-folded on win32 only,
// where the filesystem is case-insensitive, so C:\X and c:\x share one
// entry). Only successful non-empty resolutions are cached; a failure or
// timeout returns "user:global" and never caches the path. An entry expires
// at exactly TTL (age < ttl is fresh; age == ttl is a miss). The cache is
// bounded (FIFO eviction, insertion order) so a long-lived host process
// cannot grow it without limit.
const namespaceCache = new Map();
let _nsStats = { hits: 0, misses: 0 };
let _nsClock = () => Date.now();

function namespaceCacheKey(projectDir) {
    const resolved = resolve(projectDir);
    return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function clearNamespaceCache() {
    namespaceCache.clear();
    _nsStats = { hits: 0, misses: 0 };
    _nsClock = () => Date.now();
}

function namespaceCacheStats() {
    return { hits: _nsStats.hits, misses: _nsStats.misses };
}

// --- Find python (for namespace resolution) ---------------------------------
// Windows: prefer `python` (python3 is often a no-op Store stub). Verified by
// actually resolving the namespace; on failure we fall back to 'user:global'.
function resolveNamespace(projectDir, opts = {}) {
    if (!projectDir) return "user:global";
    const clock = typeof opts.clock === "function" ? opts.clock : _nsClock;
    const warn = opts.warn;
    const ttlMs = opts.ttlMs !== undefined
        ? opts.ttlMs
        : readPositiveIntMs(process.env, "ZMEM_NAMESPACE_CACHE_TTL_MS",
            budgetDefault("namespace_cache_ttl_ms", DEFAULT_NAMESPACE_CACHE_TTL_MS), warn);
    const resolveMs = opts.resolveMs !== undefined
        ? opts.resolveMs
        : readPositiveIntMs(process.env, "ZMEM_NAMESPACE_RESOLVE_MS",
            budgetDefault("namespace_resolve_ms", DEFAULT_NAMESPACE_RESOLVE_MS), warn);
    const key = namespaceCacheKey(projectDir);
    const now = clock();
    const cached = namespaceCache.get(key);
    if (cached) {
        if (now - cached.at < ttlMs) {
            _nsStats.hits++;
            return cached.ns;
        }
        namespaceCache.delete(key); // expired at exactly TTL
    }
    _nsStats.misses++;
    const scriptsDir = join(getPluginRoot(), "skills", "memory", "scripts");
    // The resolver answers in JSON: host.resolve_namespace is the SOLE
    // producer of project:* keys, and it falls back to a normalized-abspath
    // key for non-remote projects. We compare against host.py's own
    // normalizer so only genuinely remote-derived keys (issue #121: "cache
    // only successful non-empty remote namespaces ... never cache a path
    // key") enter the cache — path keys are returned uncached every time.
    const code =
        "import json, sys; sys.path.insert(0, sys.argv[1]); " +
        "from pathlib import Path; import host; " +
        "_p = Path(sys.argv[2]); _ns = host.resolve_namespace(_p); " +
        "_fb = None\n" +
        "try:\n" +
        "    _fb = 'project:' + host._norm_abspath_key(_p)\n" +
        "except Exception:\n" +
        "    _fb = None\n" +
        "print(json.dumps({'ns': _ns, 'remote': bool(_fb is None or _ns != _fb)}))";
    const candidates =
        process.platform === "win32" ? ["python", "python3"] : ["python3", "python"];
    for (const py of candidates) {
        try {
            const out = execFileSync(py, ["-c", code, scriptsDir, projectDir], {
                encoding: "utf8",
                timeout: resolveMs,
                stdio: ["ignore", "pipe", "ignore"],
            }).trim();
            if (out) {
                let resolved = out;
                let cacheable = true;
                try {
                    const parsed = JSON.parse(out);
                    if (parsed && typeof parsed.ns === "string") {
                        resolved = parsed.ns;
                        cacheable = parsed.remote === true;
                    }
                } catch {
                    // A pre-JSON resolver build printing the bare key: treat
                    // the non-empty result as cacheable (fail-safe).
                }
                if (resolved) {
                    if (cacheable) {
                        namespaceCache.set(key, { ns: resolved, at: now });
                        if (namespaceCache.size > NAMESPACE_CACHE_MAX_ENTRIES) {
                            const oldest = namespaceCache.keys().next().value;
                            namespaceCache.delete(oldest);
                        }
                    }
                    return resolved;
                }
            }
        } catch {
            // try next interpreter
        }
    }
    warnOnce(warn, "namespace_resolution_error",
        "zmem: namespace_resolution_error=1 (falling back to user:global)\n");
    return "user:global";
}

// --- Launcher watchdog (issue #121) -------------------------------------------
// Bounds a translated hook's total runtime: at the deadline the child tree is
// terminated, the LAST COMPLETE sentinel payload collected so far is emitted
// (Tier 0 on the session-start path — the whole point of the fast path), an
// outer-timeout decision record is appended, and the launcher exits 0. Normal
// close clears the timer. The clock is injectable: production uses the global
// timer functions (setTimeout returns an id, clearTimeout accepts it); tests
// inject a fake clock with the same numeric-id contract.
const PRODUCTION_CLOCK = {
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
};

let _lastTerminateInfo = { mode: "none" };

function _lastTerminateInfoForTests() {
    return { ..._lastTerminateInfo };
}

// Terminate the child AND (on win32) its spawned subtree: a bare kill of
// the bash child would orphan the python grandchildren the hook scripts
// spawn — and those grandchildren INHERIT stdio handles, so a survivor
// can hold the HOST read side open past our own exit. taskkill /T walks
// the tree; two sequencing rules keep that walk effective: (1) taskkill
// must be spawned async + detached — spawnSync("taskkill") deadlocks the
// Node event loop when the launcher own stdio are pipes (the host
// hook-runner configuration); (2) nothing may synchronously kill the
// direct child first — a dead root PID makes the tree walk fail and
// re-orphans the grandchildren. The watchdog callback bounds teardown
// with a grace window plus a child.kill() fallback. Fake children
// without a real pid skip the taskkill branch so injected-clock unit
// tests stay pure.
function _terminateChildTree(child) {
    if (process.platform === "win32" && child && typeof child.pid === "number" && child.pid > 0) {
        try {
            const tk = spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
                stdio: "ignore",
                detached: true,
                windowsHide: true,
            });
            try { tk.unref(); } catch { /* already gone */ }
            _lastTerminateInfo = { mode: "taskkill", pid: child.pid };
            return; // the tree kill is in flight; do NOT kill the root first
        } catch {
            _lastTerminateInfo = { mode: "kill-fallback", pid: child.pid };
        }
    } else {
        _lastTerminateInfo = { mode: "kill" };
    }
    try { if (child && typeof child.kill === "function") child.kill(); } catch { /* already gone */ }
}

// child may be the child object itself OR a getter evaluated at fire
// time (issue #121 F-001: the watchdog arms before the child spawns,
// so startup work counts against the budget). A null/absent child at
// fire time is a clean kill no-op.
function startWatchdog(child, timeoutMs, clock, onTimeout) {
    const c = clock || PRODUCTION_CLOCK;
    let fired = false;
    const id = c.setTimeout(() => {
        if (fired) return;
        fired = true;
        _terminateChildTree(typeof child === "function" ? child() : child);
        try { if (typeof onTimeout === "function") onTimeout(); } catch { /* fail open */ }
    }, timeoutMs);
    return {
        clear() { if (!fired) c.clearTimeout(id); },
        fired() { return fired; },
    };
}

// Data-dir chain mirrors the payload python's _data_dir() resolution
// (zmem-session-start-payload.py): ZMEM_STORE's directory > ZMEM_DATA >
// CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem, so launcher-side and
// payload-side decision lines always co-locate.
function _decisionLogDir(env) {
    const e = env || {};
    // PR #198 review F-005: mirror the python resolver — expandHome on
    // every branch, and a dirname that resolves to "." (a bare-filename
    // ZMEM_STORE like "store.sqlite") is treated as absent so the chain
    // falls through instead of writing the audit log into the current
    // working directory.
    const pick = (v) => {
        if (!v) return null;
        const expanded = expandHome(String(v));
        const dir = dirname(expanded);
        if (!dir || dir === "." || dir === expanded) return null;
        return dir;
    };
    return pick(e.ZMEM_STORE)
        || pick(e.ZMEM_DATA)
        || pick(e.CLAUDE_PLUGIN_DATA)
        || pick(e.ZCODE_PLUGIN_DATA)
        || join(homedir(), ".zmem");
}

// Append the launcher-side outer-timeout decision record. Returns the
// structured record (the tests/fixtures/timeout/expected_timeout.json shape);
// a log-write failure fails open (the record is still returned, the hook
// still exits 0).
function appendOuterTimeoutDecision(env, hookName, stage, fields = {}) {
    const e = env || {};
    const record = {
        namespace: e.ZMEM_NAMESPACE || "",
        outer_timeout: 1,
        reason: "omitted",
        stage: stage || "launcher",
        tier0_emitted: fields.tier0_emitted ? 1 : 0,
        tier2_rows: 0,
        timeout_ms: fields.timeout_ms !== undefined && fields.timeout_ms !== null
            ? fields.timeout_ms
            : DEFAULT_LAUNCHER_WATCHDOG_MS,
    };
    try {
        const dir = _decisionLogDir(e);
        try { mkdirSync(dir, { recursive: true }); } catch { /* best-effort */ }
        appendFileSync(
            join(dir, "zmem-decisions.log"),
            `[${Math.floor(Date.now() / 1000)}] zmem-hook status=silent reason=omitted `
            + `outer_timeout=1 stage=${record.stage} timeout_ms=${record.timeout_ms} `
            + `tier0_emitted=${record.tier0_emitted} tier2_rows=0 `
            + `hook=${hookName || ""} ns=${record.namespace} `
            + `sid=${e.ZMEM_SESSION || ""} `
            + `moment=${hookEventNameFor(e.ZMEM_HOST, hookName) || hookName || ""}\n`,
            "utf8"
        );
    } catch { /* fail open: the audit log never blocks the hook */ }
    return record;
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
    // Issue #118 (D-2 scope 1): SessionStart fires on every source
    // (startup | resume | clear | compact on Claude Code; source=compact on
    // Codex after each compaction). The adapter stays dumb — it exports the
    // field verbatim and the session-start shell hook owns the one branch
    // (source == "compact" → query-aware re-injection from the compact
    // sidecar). Empty on hosts that send no source field (cold-start shape).
    const sessionSource = (meta && meta.source) || "";

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
    env.ZMEM_SESSION_SOURCE = sessionSource;
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
// systemMessage (issue #107) is the OPERATOR channel: a served-tree drift
// notice shown to the user, never injected into model context. It rides
// top-level next to the additionalContext envelope for every host (hosts that
// do not render it ignore the extra key harmlessly).
function makeEnvelope(host, hookName, content, systemMessage) {
    const envelope = {};
    if (host === "claude" || host === "codex") {
        envelope.hookSpecificOutput = {
            hookEventName: hookEventNameFor(host, hookName),
            additionalContext: content,
        };
    } else {
        envelope.additionalContext = content;
    }
    if (typeof systemMessage === "string" && systemMessage) {
        envelope.systemMessage = systemMessage;
    }
    return envelope;
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
// Issue #107: the operator-facing systemMessage is read BEFORE the
// empty-content early-return — a kill-switch session emits no
// additionalContext, but its served-tree drift notice must still reach the
// user. systemMessage is not content-trimmed by fitEnvelope (it is an
// operator string, never model context), but it DOES consume budget: its
// exact encoded marginal size is reserved from the budget before the
// content is fitted, so the ASSEMBLED envelope stays within the host cap
// (issue #95 PRR-001 — the pre-#95 code appended sysMsg post-fit, which a
// child-script regression could push over the codex spill threshold). An
// operator message that alone cannot fit is dropped entirely (fail-open)
// rather than guaranteed to spill.
function translate(raw, host, hookName, budget) {
    const payload = extractPayload(raw);
    if (payload === null) return {}; // missing/invalid sentinel → fail open
    const content = payload.additionalContext;
    let sysMsg =
        typeof payload.systemMessage === "string" && payload.systemMessage.trim()
            ? payload.systemMessage
            : null;
    const hasContent = !(content === undefined || content === null || content === "");
    if (!hasContent && !sysMsg) return {};
    let contentBudget = budget;
    if (sysMsg) {
        const sysBytes = encodedSize(_withSystemMessage(makeEnvelope(host, hookName, ""), sysMsg))
            - encodedSize(makeEnvelope(host, hookName, ""));
        if (sysBytes >= budget) {
            sysMsg = null;
        } else {
            contentBudget = budget - sysBytes;
        }
    }
    const envelope = hasContent
        ? fitEnvelope(host, hookName, String(content), contentBudget)
        : makeEnvelope(host, hookName, "");
    if (sysMsg) envelope.systemMessage = sysMsg;
    return envelope;
}

// Shallow-copy helper: an envelope with ONLY the operator message attached,
// used to measure systemMessage's exact encoded marginal size.
function _withSystemMessage(envelope, sysMsg) {
    envelope.systemMessage = sysMsg;
    return envelope;
}

// Codex-only envelope cap. Upstream codex-cli spills hook output above
// DEFAULT_HOOK_OUTPUT_TOKEN_LIMIT = 2_500 tokens (codex-rs/hooks/src/
// output_spill.rs; verified 2026-09-09 against tag rust-v0.153.0 == main):
// the text is written to a temp file and the model sees only a head/tail
// preview — a spilled fence is effectively lost. At the plugin's 4-chars-
// per-token estimator, 8000 chars ≈ 2000 tokens = 20% margin under the
// spill point (the former 9000-char default sat within ~10% of it, issue
// #95's units trap). Caveat (PRR-003): the estimator is per-CHARACTER —
// dense multi-byte content (CJK) tokenizes at fewer chars per token, so
// such fences have less real headroom than the 20% figure suggests. The
// clamp is applied in main() AFTER resolveBudget, so it binds the host
// default AND any operator-set ZMEM_CTX_BUDGET on codex (an override that
// exceeds the cap is clamped WITH a stderr warning) — an override must not
// reintroduce the spill risk. Claude/ZCode are unaffected (BUDGET_DEFAULT
// stays 9000 there).
const CODEX_ENVELOPE_CAP_CHARS = 8000;

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

    // Issue #121 (F-001): the watchdog arms BEFORE any startup work —
    // the stdin wait, the canonical env build (namespace resolution can
    // stall its interpreter candidates), and the child spawn all count
    // against this budget, so TOTAL launcher runtime stays inside the
    // host hook timeout even when startup itself hangs. At fire: emit
    // the last complete sentinel collected so far (an empty {} envelope
    // if nothing arrived), append the outer-timeout decision record, and
    // exit 0. Pass-through hooks stay unwatched (documented residual).
    const translated = TRANSLATED_HOOKS.has(hookName);
    const outChunks = [];
    const fireState = { child: null, host: "", budget: 0, env: null };
    let watchdog = null;
    if (translated) {
        const watchdogMs = readPositiveIntMs(process.env, "ZMEM_LAUNCHER_WATCHDOG_MS",
            budgetDefault("launcher_watchdog_ms", DEFAULT_LAUNCHER_WATCHDOG_MS));
        watchdog = startWatchdog(() => fireState.child, watchdogMs, PRODUCTION_CLOCK, () => {
            const raw = Buffer.concat(outChunks).toString("utf8");
            let envelope;
            try {
                envelope = translate(raw, fireState.host || detectHost(), hookName,
                    fireState.budget || resolveBudget(process.env));
            } catch {
                envelope = {};
            }
            process.stdout.write(JSON.stringify(envelope) + "\n");
            appendOuterTimeoutDecision(fireState.env || process.env, hookName, "launcher", {
                tier0_emitted: hookName === "session-start" && extractPayload(raw) !== null,
                timeout_ms: watchdogMs,
            });
            const child = fireState.child;
            if (!child) {
                process.exit(0);
                return;
            }
            // Bounded teardown grace: the detached taskkill needs a moment
            // to walk the tree. The envelope above is already written, so
            // this window only affects exit latency, never delivery.
            const grace = setTimeout(() => {
                try { child.kill(); } catch { /* already gone */ }
                process.exit(0);
            }, 500);
            child.on("close", () => {
                clearTimeout(grace);
                process.exit(0);
            });
        });
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
    let budget = resolveBudget(env);
    if (host === "codex" && budget > CODEX_ENVELOPE_CAP_CHARS) {
        // PRR-002 (#95): an explicitly-set operator budget above the codex
        // host cap gets clamped — say so on stderr instead of shrinking it
        // silently. The 9000 host DEFAULT also exceeds the cap but is not
        // operator-set, so it must not warn on every codex run.
        if (process.env.ZMEM_CTX_BUDGET) {
            process.stderr.write(`zmem: ZMEM_CTX_BUDGET=${budget} exceeds the `
                + `codex envelope cap ${CODEX_ENVELOPE_CAP_CHARS}; clamping\n`);
        }
        budget = CODEX_ENVELOPE_CAP_CHARS;
    }
    // Export the validated/clamped budget so spawned hook scripts see the same
    // effective value the launcher uses internally (#39 E3 / cubic-re #1).
    // Without this, a huge operator-set ZMEM_CTX_BUDGET is clamped for
    // fitEnvelope but propagated unclamped to child shell scripts that read
    // $ZMEM_CTX_BUDGET directly.
    env.ZMEM_CTX_BUDGET = String(budget);
    const bashPath = findBash();

    // Translated hooks: buffer child stdout so we can rewrap it. Pass-through
    // hooks: inherit stdout/stderr so their output reaches the runner
    // unchanged. For translated hooks the child stderr is a LAUNCHER-OWNED
    // pipe we pump below (issue #121): grandchildren inherit handles from
    // the child, and an inherited stderr would let a survivor hold the
    // HOST read side open past our own exit — with a launcher-owned pipe,
    // the host sees EOF the moment we exit no matter what the tree kill
    // reached.
    const child = spawn(bashPath, [scriptPath], {
        stdio: ["pipe", translated ? "pipe" : "inherit", translated ? "pipe" : "inherit"],
        env: buildChildEnv(env, bashPath),
    });
    if (translated && child.stderr) {
        child.stderr.on("data", (c) => {
            try { process.stderr.write(c); } catch { /* host stderr gone */ }
        });
    }

    // The watchdog (armed above, before startup) now binds the child:
    // everything before this line already consumed its budget.
    if (translated) fireState.child = child;
    fireState.host = host;
    fireState.budget = budget;
    fireState.env = env;

    // Replay the exact original stdin bytes to the child, then close its stdin.
    // Guard EPIPE/ECONNRESET: session-start never reads stdin, so end() can hit
    // an already-closed pipe — swallow it rather than crash the launcher.
    if (child.stdin) {
        child.stdin.on("error", () => {});
        child.stdin.write(prepared.input);
        child.stdin.end();
    }

    if (translated && child.stdout) {
        child.stdout.on("data", (c) => outChunks.push(c));
        child.on("close", (code) => {
            if (watchdog) watchdog.clear(); // normal close: disarm the watchdog
            if (watchdog && watchdog.fired()) {
                // The watchdog already emitted the retained envelope
                // and wrote the decision record — never double-emit.
                process.exit(0);
                return;
            }
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
    namespaceCacheStats,
    clearNamespaceCache,
    readPositiveIntMs,
    startWatchdog,
    appendOuterTimeoutDecision,
    _decisionLogDir,
    _lastTerminateInfoForTests,
    _terminateChildTree,
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
    CODEX_ENVELOPE_CAP_CHARS,
    DEFAULT_LAUNCHER_WATCHDOG_MS,
    DEFAULT_NAMESPACE_RESOLVE_MS,
    DEFAULT_NAMESPACE_CACHE_TTL_MS,
    NAMESPACE_CACHE_MAX_ENTRIES,
    EVENT_MAP,
    TRANSLATED_HOOKS,
    NEEDS_NAMESPACE,
};
