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
const { createHash, randomUUID } = require("crypto");
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
const MAX_HOOK_INPUT_BYTES = 256 * 1024;

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
    // every branch. ZMEM_STORE is a FILE (use its dirname; a bare filename
    // like "store.sqlite" yields "." and is treated as absent), while
    // ZMEM_DATA / plugin-data vars ARE the directory (used verbatim).
    const pickStore = (v) => {
        if (!v) return null;
        const expanded = expandHome(String(v));
        const dir = dirname(expanded);
        if (!dir || dir === "." || dir === expanded) return null;
        return dir;
    };
    const pickDir = (v) => (v ? expandHome(String(v)) : null);
    return pickStore(e.ZMEM_STORE)
        || pickDir(e.ZMEM_DATA)
        || pickDir(e.CLAUDE_PLUGIN_DATA)
        || pickDir(e.ZCODE_PLUGIN_DATA)
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

// Host adapters can expose a successful top-level status while a nested
// result/tool_result reports the actual failure.  Inspect every supported
// status/error carrier before deciding whether an event is successful; a
// success must never erase a sibling or nested failure signal.
function isMeaningfulFailureValue(value) {
    if (typeof value === "number") return Number.isFinite(value) && value !== 0;
    if (value === true) return true;
    if (typeof value === "string") return Boolean(value.trim());
    if (Array.isArray(value)) return value.length > 0;
    if (value && typeof value === "object") {
        const message = typeof value.message === "string" ? value.message.trim() : "";
        const type = typeof value.type === "string" ? value.type.trim() : "";
        return Boolean(message || type || Object.keys(value).length);
    }
    return false;
}

function failureSignals(...values) {
    const statuses = [];
    const errors = [];
    const seen = new Set();
    const visit = (value, depth = 0) => {
        if (!value || typeof value !== "object" || depth > 8 || seen.has(value)) return;
        seen.add(value);
        for (const key of ["status", "tool_status", "toolStatus"]) {
            if (typeof value[key] === "string" && value[key].trim()) {
                statuses.push(value[key]);
            }
        }
        for (const key of [
            "error", "tool_error", "toolError", "error_message", "error_type", "failure",
        ]) {
            if (value[key] !== undefined && value[key] !== null && value[key] !== "") {
                errors.push(value[key]);
            }
        }
        for (const key of ["result", "tool_result", "tool_output", "cause", "details"]) {
            visit(value[key], depth + 1);
        }
        seen.delete(value);
    };
    for (const value of values) visit(value);
    return {
        failed: statuses.some(isFailureStatus) || errors.some(isMeaningfulFailureValue),
    };
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

function firstMeaningfulError(...values) {
    for (const value of values) {
        const normalized = normalizeErrorValue(value);
        if (normalized) return normalized;
    }
    return null;
}

// Codex failure capture runs on PostToolUse because there is no dedicated
// PostToolUseFailure event. Normalize the stable PostToolUse payload into the
// shape the existing capture-failure hook script already understands. If the
// payload does not clearly describe a failure, return null and fail open.
function normalizeCodexFailurePayload(meta) {
    if (!meta || typeof meta !== "object") return null;

    const signals = failureSignals(meta);
    const failed = signals.failed;

    const error = firstMeaningfulError(
        meta.error, meta.tool_error, meta.toolError, meta.error_message, meta.error_type,
        meta.result && meta.result.error, meta.result && meta.result.error_message,
        meta.result && meta.result.error_type, meta.tool_result && meta.tool_result.error,
        meta.tool_result && meta.tool_result.error_message,
        meta.tool_result && meta.tool_result.error_type,
        meta.tool_output && meta.tool_output.error,
        meta.tool_output && meta.tool_output.error_message,
        meta.tool_output && meta.tool_output.error_type,
        failed && firstNonEmpty(
            meta.stderr,
            meta.message,
            meta.failure,
            meta.tool_message,
            meta.toolMessage,
            meta.result && meta.result.message,
            meta.result && meta.result.error_message,
            meta.result && meta.result.error_type,
            meta.tool_result && meta.tool_result.message,
            meta.tool_result && meta.tool_result.error_message,
            meta.tool_result && meta.tool_result.error_type,
            meta.tool_output && meta.tool_output.message,
            meta.tool_output && meta.tool_output.error_message,
            meta.tool_output && meta.tool_output.error_type,
        ),
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

// --- Detached evidence writer ------------------------------------------------
// Evidence is observational and must never sit on the host delivery path.  The
// launcher sends only the normalized, bounded row to the store CLI; the store
// owns redaction, the final 400-character cap, hashing, and its writer lease.
const EVIDENCE_HOSTS = new Set(["claude", "codex", "zcode"]);
const EVIDENCE_RAW_MAX_BYTES = 64 * 1024;
const EVIDENCE_WRITER_MAX_INFLIGHT = 8;
const EVIDENCE_WRITER_TIMEOUT_MS = 15000;
let evidenceWritersInFlight = 0;
const EDIT_TOOL_NAMES = new Set([
    "edit", "edit_file", "write", "write_file", "writefile", "notebookedit",
    "notebook_edit", "multiedit", "multi_edit", "applypatch", "apply_patch",
    "patchfile", "patch_file", "strreplaceeditor", "str_replace_editor",
]);

function canonicalUtcNow(date = new Date()) {
    return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}

function _firstNonEmptyString(...values) {
    for (const value of values) {
        if (typeof value === "string" && value.trim()) return value.trim();
    }
    return "";
}

function _compactJson(value) {
    try {
        return safeJsonStringify(value, EVIDENCE_RAW_MAX_BYTES);
    } catch {
        return null;
    }
}

function _stableEvidenceId(meta, payload) {
    const suppliedId = _firstNonEmptyString(meta.evidence_id, payload.evidence_id);
    if (/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$/.test(suppliedId)) {
        return suppliedId;
    }
    const taskId = _firstNonEmptyString(
        meta.task_id, meta.taskId, payload.task_id, payload.taskId,
    );
    const callId = _firstNonEmptyString(
        meta.tool_call_id, meta.toolCallId, payload.tool_call_id, payload.toolCallId,
    );
    if (!taskId || !callId) return randomUUID();
    // Match Python's uuid.uuid5(uuid.NAMESPACE_URL, name) so the host hooks
    // converge on one evidence identity when the same tool call is observed.
    const namespace = Buffer.from("6ba7b8119dad11d180b400c04fd430c8", "hex");
    const digest = createHash("sha1")
        .update(namespace)
        .update(`zmem-hermes:${taskId}:${callId}`, "utf8")
        .digest();
    digest[6] = (digest[6] & 0x0f) | 0x50;
    digest[8] = (digest[8] & 0x3f) | 0x80;
    const hex = digest.subarray(0, 16).toString("hex");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Refuse hostile/cyclic JSON-shaped values before JSON.stringify can walk a
// giant object graph or invoke surprising coercions.  This is an admission
// check, not a truncator: observational evidence is dropped when it cannot be
// represented safely within the transport bound.
function _boundedJsonShape(value, budget, depth = 0, seen = new Set()) {
    if (budget < 0 || depth > 32) return false;
    if (value === null || typeof value === "boolean") return 4 <= budget;
    if (typeof value === "number") return Number.isFinite(value) && String(value).length <= budget;
    if (typeof value === "string") return value.length <= budget && !/[\ud800-\udfff]/.test(value);
    if (!value || typeof value !== "object" || seen.has(value)) return false;
    seen.add(value);
    try {
        const entries = Array.isArray(value) ? value : Object.entries(value);
        if (entries.length > 256) return false;
        let used = 2;
        for (const entry of entries) {
            const key = Array.isArray(value) ? null : entry[0];
            const item = Array.isArray(value) ? entry : entry[1];
            if (key !== null && (typeof key !== "string" || key.length > budget)) return false;
            if (!_boundedJsonShape(item, budget - used, depth + 1, seen)) return false;
            used += (key === null ? 0 : key.length + 3) + 4;
            if (used > budget) return false;
        }
        return true;
    } finally {
        seen.delete(value);
    }
}

function safeJsonStringify(value, maxBytes = Number.POSITIVE_INFINITY) {
    if (Number.isFinite(maxBytes) && !_boundedJsonShape(value, maxBytes)) return null;
    try {
        const json = JSON.stringify(value);
        if (typeof json !== "string") return null;
        const safe = json.replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
        return Number.isFinite(maxBytes) && Buffer.byteLength(safe, "utf8") > maxBytes
            ? null : safe;
    } catch {
        return null;
    }
}

function _sanitizeRefPath(value) {
    if (typeof value !== "string") return "";
    let clean = value.replace(/[\u0000-\u001f\u007f\u2028\u2029]/g, " ").trim();
    if (!clean.includes("://") && (/^[\\/]/.test(clean) || /^[A-Za-z]:[\\/]/.test(clean))) {
        clean = basename(clean.replaceAll("\\", "/"));
    }
    return clean.slice(0, 4096);
}

function resolvePython(env = process.env) {
    const explicit = env && typeof env.ZMEM_PYTHON === "string" ? env.ZMEM_PYTHON.trim() : "";
    if (explicit) return explicit;
    const candidates = process.platform === "win32" ? ["python", "python3"] : ["python3", "python"];
    for (const candidate of candidates) {
        try {
            execFileSync("where", [candidate], { stdio: "ignore" });
            return candidate;
        } catch {
            // Try the next interpreter name; the caller remains fail-open.
        }
    }
    return candidates[0];
}

function _patchPath(value) {
    if (typeof value !== "string" || !value.trim()) return "";
    const text = value.replace(/\r/g, "");
    const markers = [
        /^[ \t]*\*\*\*[ \t]+(?:Update|Add|Delete)[ \t]+File:[ \t]*(\S.*?)[ \t]*$/m,
        /^[ \t]*\+\+\+[ \t]+(?:b\/)?([^\s]+)[ \t]*$/m,
        /^[ \t]*---[ \t]+(?:a\/)?([^\s]+)[ \t]*$/m,
    ];
    for (const marker of markers) {
        const match = marker.exec(text);
        if (match && match[1]) return match[1].trim();
    }
    return "";
}

function _editPath(toolInput) {
    if (typeof toolInput === "string") return _patchPath(toolInput);
    if (!toolInput || typeof toolInput !== "object" || Array.isArray(toolInput)) return "";
    const direct = _firstNonEmptyString(
        toolInput.file_path,
        toolInput.filePath,
        toolInput.path,
        toolInput.notebook_path,
        toolInput.notebookPath,
        toolInput.target_file,
        toolInput.targetFile,
        toolInput.filename,
    );
    if (direct) return direct;
    for (const entry of [toolInput.edits, toolInput.files, toolInput.changes]) {
        if (!Array.isArray(entry)) continue;
        for (const item of entry) {
            const nested = _editPath(item);
            if (nested) return nested;
        }
    }
    for (const value of [toolInput.patch, toolInput.patch_text, toolInput.patchText,
        toolInput.diff, toolInput.input, toolInput.content]) {
        const patchPath = _patchPath(value);
        if (patchPath) return patchPath;
    }
    return "";
}

function _isEditTool(toolName, toolInput) {
    const normalized = String(toolName || "").toLowerCase().replace(/[\s-]+/g, "_");
    return EDIT_TOOL_NAMES.has(normalized) && Boolean(_editPath(toolInput));
}

function _evidenceExcerpt(kind, hookName, payload, toolInput) {
    if (kind === "turn") {
        const result = _firstNonEmptyString(payload.result, payload.stop_reason, payload.status);
        return result ? `turn=${result}` : `turn=${hookName}`;
    }
    if (kind === "tool_failure") {
        const error = firstMeaningfulError(
            payload.error, payload.error_message, payload.message,
            payload.failure, payload.tool_result && payload.tool_result.message,
        );
        if (typeof error === "string") return `error=${error}`;
        if (error && typeof error === "object") {
            const detail = firstNonEmpty(error.message, error.type);
            return detail ? `error=${detail}` : "tool failure";
        }
        return "tool failure";
    }
    if (toolInput && typeof toolInput === "object") {
        const command = _firstNonEmptyString(
            toolInput.command, toolInput.cmd, toolInput.file_path,
            toolInput.path, toolInput.notebook_path,
        );
        if (command) return command;
        const compact = _compactJson(toolInput);
        if (compact) return compact;
    }
    return _firstNonEmptyString(payload.excerpt, payload.result, hookName) || hookName;
}

function recordEvidence(host, hookName, payload, meta, env = process.env,
                        clock = canonicalUtcNow, spawnFn = spawn) {
    try {
        if (!EVIDENCE_HOSTS.has(host) || !["convention-capture", "capture-failure", "reflect"].includes(hookName)) {
            return false;
        }
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
        if (!meta || typeof meta !== "object" || Array.isArray(meta)) meta = {};
        const raw = _compactJson({ payload, meta });
        if (!raw || Buffer.byteLength(raw, "utf8") > EVIDENCE_RAW_MAX_BYTES) return false;

        const toolName = _firstNonEmptyString(
            payload.tool_name, payload.toolName, meta.tool_name, meta.toolName,
        );
        const toolInput = payload.tool_input || payload.toolInput || payload.arguments
            || (payload.tool && payload.tool.input) || {};
        let kind;
        if (hookName === "reflect") kind = "turn";
        else if (hookName === "capture-failure") kind = "tool_failure";
        else {
            const failed = failureSignals(payload, meta).failed;
            // Codex routes successful and failed PostToolUse events through the
            // same matcher.  The failure observer records the failure event;
            // convention-capture must not create a second successful edit row.
            if (failed) return false;
            kind = _isEditTool(toolName, toolInput) ? "edit" : "tool_call";
        }

        const sessionId = _firstNonEmptyString(
            meta.session_id, meta.sessionId, payload.session_id, payload.sessionId,
            env && env.ZMEM_SESSION,
        );
        if (!sessionId) return false;
        let refPath = _firstNonEmptyString(
            (kind === "edit" ? _editPath(toolInput) : ""),
            (kind === "edit" ? _patchPath(payload.patch || payload.diff || "") : ""),
            payload.ref_path, meta.ref_path,
            payload.transcript_path, meta.transcript_path,
            payload.transcriptPath, meta.transcriptPath,
        );
        if (!refPath) refPath = `host://${host}/${hookName}/unavailable`;
        refPath = _sanitizeRefPath(refPath);
        if (!refPath) return false;
        const excerpt = _evidenceExcerpt(kind, hookName, payload, toolInput);
        if (!excerpt || Buffer.byteLength(excerpt, "utf8") > EVIDENCE_RAW_MAX_BYTES) return false;
        const id = _stableEvidenceId(meta, payload);
        const row = {
            session_id: sessionId,
            lane: host,
            // A Stop/reflect row belongs to the completed user turn, not the
            // beginning of a session.
            moment: kind === "turn" ? "user_prompt" : "pretool",
            kind,
            ts: typeof clock === "function" ? clock() : canonicalUtcNow(),
            excerpt,
            ref_path: refPath,
            ref_offset: null,
            id,
        };
        const input = _compactJson(row);
        if (!input || Buffer.byteLength(input, "utf8") > EVIDENCE_RAW_MAX_BYTES) return false;
        const root = (env && (env.ZMEM_ROOT || env.PLUGIN_ROOT ||
            env.CLAUDE_PLUGIN_ROOT || env.ZCODE_PLUGIN_ROOT)) || getPluginRoot();
        const storePy = join(root, "skills", "memory", "scripts", "store.py");
        if (!existsSync(storePy) || typeof spawnFn !== "function") return false;
        const childEnv = { ...(env || {}) };
        childEnv.ZMEM_HOST = host;
        childEnv.ZMEM_MODEL_AUTODOWNLOAD = "0";
        // Observers must never initialize a missing store merely because a
        // host event was delivered; the CLI turns this into a silent no-op.
        childEnv.ZMEM_EVIDENCE_NO_CREATE = "1";
        const py = resolvePython(env);
        if (evidenceWritersInFlight >= EVIDENCE_WRITER_MAX_INFLIGHT) return false;
        const child = spawnFn(py, [storePy, "evidence", "write"], {
            env: childEnv,
            stdio: ["pipe", "ignore", "ignore"],
            detached: true,
        });
        if (!child) return false;
        evidenceWritersInFlight += 1;
        let released = false;
        const release = () => {
            if (released) return;
            released = true;
            evidenceWritersInFlight = Math.max(0, evidenceWritersInFlight - 1);
        };
        try {
            if (typeof child.on === "function") {
                child.on("error", release);
                child.on("close", release);
            }
        } catch { /* fail open */ }
        const reaper = setTimeout(() => {
            try { if (typeof child.kill === "function") child.kill(); } catch { /* fail open */ }
            release();
        }, EVIDENCE_WRITER_TIMEOUT_MS);
        try { if (typeof reaper.unref === "function") reaper.unref(); } catch { /* already gone */ }
        const inputStream = child.stdin;
        if (inputStream) {
            try { if (typeof inputStream.on === "function") inputStream.on("error", () => {}); } catch { /* fail open */ }
            try { inputStream.write(input); inputStream.end(); } catch { /* child failed */ }
            try { if (typeof inputStream.unref === "function") inputStream.unref(); } catch { /* already gone */ }
        }
        try { if (typeof child.unref === "function") child.unref(); } catch { /* already gone */ }
        return true;
    } catch {
        return false;
    }
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

// --- Resolve shell (issue #186) ---------------------------------------------
// Priority: explicit env > Git Bash at known locations > derive from git > bare 'bash'
// Exported for tests; `ZMEM_BASH_PATH` is read at call time.
function resolveShell() {
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

// Convert a native wrapper-script path into the argument handed to bash
// (issue #186). Narrow-contract helper for the launcher's fixed
// `<plugin-root>/hooks/zmem-<verb>.sh` shape — NOT a general Windows→POSIX
// path converter. On Windows, absolute drive, forward-slash, UNC, and
// already-relative inputs all normalize to `hooks/<script>.sh`, which the
// child resolves against its cwd (`cwd: getPluginRoot()` in main()). On
// non-Windows the absolute input is returned unchanged. Pure: no fs access.
function toBashPath(filePath) {
    if (process.platform !== "win32") return filePath;
    const normalized = String(filePath).split("\\").join("/");
    if (!/^[A-Za-z]:\//.test(normalized) && !normalized.startsWith("//")) {
        return normalized;
    }
    // Absolute: keep the final two segments (`hooks/<script>.sh`). The launcher
    // only ever passes join(getPluginRoot(), "hooks", "zmem-<verb>.sh").
    const baseSlash = normalized.lastIndexOf("/", normalized.length - 2);
    const dirSlash = normalized.lastIndexOf("/", baseSlash - 1);
    return normalized.slice(dirSlash + 1);
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
// user. Issue #154: the budget is a property of the COMPLETED envelope, so
// every branch measures the finished candidate with encodedSize() instead of
// a projection of it (the pre-#154 code reserved only the message's encoded
// marginal size, which ignored the base envelope overhead on the
// message-only branch and squeezed content into a degenerate budget when the
// marginal was near the cap). Preference order when both channels are
// present: completed envelope with message → content-only re-fit at the
// full budget → fitEnvelope's existing marker result → {}. fitEnvelope
// remains responsible for the marker and final empty-object fallbacks. An
// operator message that cannot co-fit with the content is dropped entirely
// (fail-open) rather than guaranteed to spill; a message-only payload that
// alone cannot fit emits the empty-content envelope (the notice could not be
// delivered either way). Budget post-condition (issue #154): for every
// budget in which any JSON envelope is representable (>= 2 bytes — the
// serialized {} floor; below that fitEnvelope's documented fail-open ladder
// bottoms out at {} regardless of input), every return after a valid
// payload satisfies encodedSize(result) <= budget — in particular at the
// codex cap this issue enforces (8000).
function translate(raw, host, hookName, budget) {
    if (raw === null || raw === undefined) return {}; // null/undefined raw (never-throws docblock hardening) → fail open
    const payload = extractPayload(raw);
    if (payload === null) return {}; // missing/invalid sentinel → fail open
    const content = payload.additionalContext;
    const sysMsg =
        typeof payload.systemMessage === "string" && payload.systemMessage.trim()
            ? payload.systemMessage
            : null;
    const hasContent = !(content === undefined || content === null || content === "");
    if (!hasContent && !sysMsg) return {};

    if (!hasContent) {
        // Message-only: the completed envelope IS the candidate — measure it.
        const candidate = makeEnvelope(host, hookName, "", sysMsg);
        if (encodedSize(candidate) <= budget) return candidate;
        return fitEnvelope(host, hookName, "", budget);
    }

    const contentOnly = fitEnvelope(host, hookName, String(content), budget);
    if (!sysMsg) return contentOnly;
    // Shallow copy is mandatory: _withSystemMessage attaches the key to the
    // object passed in, and the drop path below returns the untouched
    // contentOnly envelope.
    const candidate = _withSystemMessage(Object.assign({}, contentOnly), sysMsg);
    if (encodedSize(candidate) <= budget) return candidate;
    return contentOnly;
}

// Attach the operator message key onto an envelope (mutates and returns the
// envelope passed in). Callers building a measured candidate must pass a
// shallow copy whenever the original envelope is also returned on another
// path (issue #154 translate()).
function _withSystemMessage(envelope, sysMsg) {
    envelope.systemMessage = sysMsg;
    return envelope;
}

// Codex-only envelope cap. Upstream codex-cli spills hook output above
// DEFAULT_HOOK_OUTPUT_TOKEN_LIMIT = 2_500 tokens (codex-rs/hooks/src/
// output_spill.rs; verified 2026-09-09 against tag rust-v0.153.0 == main):
// the text is written to a temp file and the model sees only a head/tail
// preview — a spilled fence is effectively lost. At the plugin's 4-chars-
// per-token estimator, 8000 encoded UTF-8 bytes ≈ 2000 tokens = 20% margin
// under the spill point (the former 9000-char default sat within ~10% of
// it, issue #95's units trap). Caveat (PRR-003): the estimator is per-
// CHARACTER — dense multi-byte content (CJK) tokenizes at fewer chars per
// token, so such fences have less real headroom than the 20% figure
// suggests. The cap is encoded BYTES (issue #154 — the completed-envelope
// check and every budget comparison measure Buffer.byteLength(…, "utf8"));
// the _CHARS name under-described that contract and is retained for one
// release as a numeric alias. The clamp is applied in main() AFTER
// resolveBudget, so it binds the host default AND any operator-set
// ZMEM_CTX_BUDGET on codex (an override that exceeds the cap is clamped
// WITH a stderr warning) — an override must not reintroduce the spill risk.
// Claude/ZCode are unaffected (BUDGET_DEFAULT stays 9000 there).
const CODEX_ENVELOPE_CAP_BYTES = 8000;
const CODEX_ENVELOPE_CAP_CHARS = CODEX_ENVELOPE_CAP_BYTES;
// Issue #188: the manifest's `additionalContextLimit` is the token projection
// of the byte cap under the plugin's shared 4-chars-per-token estimator
// (skills/memory/scripts/storelib/inject.py CHARS_PER_TOKEN). Exported so the
// manifest contract test can prove floor(8000 / 4) == 2000 against the live
// launcher constants instead of a duplicated literal.
const CHARS_PER_TOKEN = 4;
const CODEX_ADDITIONAL_CONTEXT_LIMIT =
    Math.floor(CODEX_ENVELOPE_CAP_BYTES / CHARS_PER_TOKEN);

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
        let total = 0;
        let overflow = false;
        process.stdin.on("data", (c) => {
            if (overflow) return;
            total += c.length;
            if (total > MAX_HOOK_INPUT_BYTES) {
                overflow = true;
                return;
            }
            chunks.push(c);
        });
        process.stdin.on("end", () => resolve(overflow ? null : Buffer.concat(chunks)));
        process.stdin.on("error", () => resolve(overflow ? null : Buffer.concat(chunks)));
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
                    fireState.budget
                    || (process.env.ZMEM_CTX_BUDGET ? resolveBudget(process.env) : 9000));
            } catch {
                envelope = {};
            }
            process.stdout.write((safeJsonStringify(envelope) || "{}") + "\n");
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
    if (!Buffer.isBuffer(stdinBuf)) {
        process.stdout.write("{}\n");
        process.exit(0);
        return;
    }

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
    if (host === "codex" && budget > CODEX_ENVELOPE_CAP_BYTES) {
        // PRR-002 (#95): an explicitly-set operator budget above the codex
        // host cap gets clamped — say so on stderr instead of shrinking it
        // silently. The 9000 host DEFAULT also exceeds the cap but is not
        // operator-set, so it must not warn on every codex run.
        if (process.env.ZMEM_CTX_BUDGET) {
            process.stderr.write(`zmem: ZMEM_CTX_BUDGET=${budget} exceeds the `
                + `codex envelope cap ${CODEX_ENVELOPE_CAP_BYTES}; clamping\n`);
        }
        budget = CODEX_ENVELOPE_CAP_BYTES;
    }
    // Export the validated/clamped budget so spawned hook scripts see the same
    // effective value the launcher uses internally (#39 E3 / cubic-re #1).
    // Without this, a huge operator-set ZMEM_CTX_BUDGET is clamped for
    // fitEnvelope but propagated unclamped to child shell scripts that read
    // $ZMEM_CTX_BUDGET directly.
    env.ZMEM_CTX_BUDGET = String(budget);

    // Evidence is captured exactly once, after payload normalization and
    // before the delivery child is spawned.  The detached writer never awaits
    // or mutates the translated payload bytes.
    try {
        recordEvidence(host, hookName, prepared.meta, prepared.meta, env);
    } catch {
        // Observational evidence is fail-open by contract.
    }
    const bashPath = resolveShell();
    const bashScriptPath = toBashPath(scriptPath);

    // Translated hooks: buffer child stdout so we can rewrap it. Pass-through
    // hooks: inherit stdout/stderr so their output reaches the runner
    // unchanged. For translated hooks the child stderr is a LAUNCHER-OWNED
    // pipe we pump below (issue #121): grandchildren inherit handles from
    // the child, and an inherited stderr would let a survivor hold the
    // HOST read side open past our own exit — with a launcher-owned pipe,
    // the host sees EOF the moment we exit no matter what the tree kill
    // reached.
    // Final critic: spawn() can either emit an async error event (ENOENT
    // on POSIX) or THROW synchronously (Windows EFTYPE for a
    // non-executable bash path) — both fail open: clear the watchdog,
    // emit an empty envelope, exit 0. The old error handler was lost in
    // the F-001 restructure and the launcher crashed (exit 1, zero
    // stdout) on this path.
    let child;
    try {
        child = spawn(bashPath, [bashScriptPath], {
            stdio: ["pipe", translated ? "pipe" : "inherit", translated ? "pipe" : "inherit"],
            env: buildChildEnv(env, bashPath),
            // Issue #186: bashScriptPath is plugin-root-relative on Windows, so
            // the child must resolve it against the plugin root. The wrappers
            // self-locate via $(dirname "$0")/BASH_SOURCE, so every downstream
            // absolute recomputation still lands on the plugin tree.
            cwd: getPluginRoot(),
        });
    } catch (spawnErr) {
        if (watchdog) watchdog.clear();
        process.stdout.write("{}\n");
        process.exit(0);
    }
    // Spawn failure (bash not found, ENOEXEC/EACCES): fail open — clear the
    // watchdog, emit an empty envelope, exit 0. spawn() delivers these as an
    // ASYNC 'error' event on POSIX (ENOENT) and a synchronous throw on
    // Windows (EFTYPE for a non-executable bash path); the try/catch above
    // covers only the sync leg, this handler the async one. History: the
    // F-001 restructure dropped the handler once before (unhandled 'error'
    // crashed the launcher, exit 1, zero stdout) and PR #210's exec-form
    // restructure dropped both registrations again — caught by the PR
    // review (cubic P1 / Copilot / swarm-pr-review PRR-001) because the
    // spawn-failure fail-open pin in tests/test_timeout_budget.py was
    // dormant in CI. Exactly ONE handler lives here.
    child.on("error", () => {
        if (watchdog) watchdog.clear();
        process.stdout.write("{}\n");
        process.exit(0);
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
            process.stdout.write((safeJsonStringify(envelope) || "{}") + "\n");
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
    resolveShell,
    toBashPath,
    hookEventNameFor,
    normalizeCodexFailurePayload,
    prepareHookPayload,
    failureSignals,
    safeJsonStringify,
    resolvePython,
    canonicalUtcNow,
    recordEvidence,
    extractPayload,
    makeEnvelope,
    encodedSize,
    fitEnvelope,
    translate,
    resolveBudget,
    CODEX_ENVELOPE_CAP_BYTES,
    CODEX_ENVELOPE_CAP_CHARS,
    CHARS_PER_TOKEN,
    CODEX_ADDITIONAL_CONTEXT_LIMIT,
    DEFAULT_LAUNCHER_WATCHDOG_MS,
    DEFAULT_NAMESPACE_RESOLVE_MS,
    DEFAULT_NAMESPACE_CACHE_TTL_MS,
    NAMESPACE_CACHE_MAX_ENTRIES,
    EVENT_MAP,
    TRANSLATED_HOOKS,
    NEEDS_NAMESPACE,
};
