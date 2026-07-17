#!/usr/bin/env node
// zmem-launch.js — Cross-platform hook launcher for ZMem.
//
// PROBLEM: ZCode's hook runner resolves the shell for `type: "command"` hooks
// via cmd.exe on Windows, which finds WSL's bash.exe before Git Bash. WSL bash
// cannot run the hook scripts (no cygpath, incompatible path resolution).
//
// SOLUTION: This Node.js launcher is invoked instead of `bash`. Node.js is a
// real .exe on the system PATH (no shell-resolution ambiguity). It finds the
// correct bash, then execs the target hook script under it.
//
// Usage in hooks.json:
//   "command": "node \"${ZCODE_PLUGIN_ROOT}/hooks/zmem-launch.js\" <hook-name> <session-start|recall|reflect>"
//
// The <hook-name> argument selects which hook script to run. The launcher
// resolves ${ZCODE_PLUGIN_ROOT} (already expanded by the runner), finds bash,
// and spawns: bash <plugin-root>/hooks/zmem-<hook-name>.sh

"use strict";

const { spawn } = require("child_process");
const { existsSync } = require("fs");
const { join, dirname } = require("path");
const { homedir } = require("os");

// --- Parse args ---
const hookName = process.argv[2];
if (!hookName) {
    // No hook name — can't proceed. Fail open (empty JSON, exit 0).
    process.stdout.write("{}\n");
    process.exit(0);
}

// --- Resolve plugin root ---
const pluginRoot = process.env.ZCODE_PLUGIN_ROOT || process.env.CLAUDE_PLUGIN_ROOT || dirname(__dirname);
const scriptPath = join(pluginRoot, "hooks", `zmem-${hookName}.sh`);

if (!existsSync(scriptPath)) {
    // Target script missing — fail open.
    process.stdout.write("{}\n");
    process.exit(0);
}

// --- Find bash ---
// Priority: explicit env > Git Bash at known locations > derive from git > bare 'bash'
function findBash() {
    // 1. Explicit override via env (ZMEM_BASH_PATH only — NOT $SHELL, which on
    //    macOS defaults to /bin/zsh and the hook scripts use bash-only constructs).
    const envBash = process.env.ZMEM_BASH_PATH;
    if (envBash && existsSync(envBash)) return envBash;

    // 2. On non-Windows, bare 'bash' works (system bash, no WSL conflict)
    if (process.platform !== "win32") return "bash";

    // 3. On Windows, try known Git Bash locations (most common first)
    const candidates = [
        // Standard Git for Windows
        "C:\\Program Files\\Git\\usr\\bin\\bash.exe",
        "C:\\Program Files\\Git\\bin\\bash.exe",
        // User-local Git install
        join(homedir(), "AppData", "Local", "Programs", "Git", "usr", "bin", "bash.exe"),
        join(homedir(), "AppData", "Local", "Programs", "Git", "bin", "bash.exe"),
        // 32-bit Program Files
        "C:\\Program Files (x86)\\Git\\usr\\bin\\bash.exe",
        "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
    ];

    for (const c of candidates) {
        if (existsSync(c)) return c;
    }

    // 4. Try to derive from git on PATH.
    // Git for Windows has multiple layouts — handle all of them:
    //   <GitRoot>\mingw64\bin\git.exe  (3 levels deep → 3 dirnames)
    //   <GitRoot>\cmd\git.exe          (2 levels deep → 2 dirnames)
    //   <GitRoot>\bin\git.exe          (2 levels deep → 2 dirnames)
    try {
        const { execSync } = require("child_process");
        const gitPath = execSync("where git", { encoding: "utf8", timeout: 3000 }).trim().split("\n")[0].trim();
        if (gitPath && existsSync(gitPath)) {
            const gitDir = dirname(gitPath);        // ...\bin or ...\cmd or ...\mingw64\bin
            const maybeRoot = dirname(gitDir);       // ...\Git (for cmd/bin) or ...\mingw64
            // Try both 2-dirname and 3-dirname derivations to cover all layouts
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

    // 5. Last resort: bare 'bash' (will be WSL on Windows, but we tried)
    return "bash";
}

const bashPath = findBash();

// --- Spawn the hook script under bash ---
// Pass through stdin (the hook JSON payload), stdout, stderr.
const child = spawn(bashPath, [scriptPath], {
    stdio: ["inherit", "inherit", "inherit"],
    env: { ...process.env },
});

child.on("error", () => {
    // Spawn failed (bash not found, etc.) — fail open.
    process.stdout.write("{}\n");
    process.exit(0);
});

child.on("exit", (code) => {
    process.exit(code || 0);
});
