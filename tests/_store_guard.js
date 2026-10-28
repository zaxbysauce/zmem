#!/usr/bin/env node
/**
 * JS test-harness store isolation guard (issue #244) — the node twin of
 * tests/_store_guard.py. Fires (exit 1) when the store path the resolution
 * chain WOULD use — explicitly env-named or not — lands inside the real
 * home's .zmem area while the default STORE FILE exists. Ephemeral CI homes
 * may have ~/.zmem — including the store file — as a side effect, so the
 * guard is silent when CI or GITHUB_ACTIONS carries a TRUTHY value (a
 * developer's CI=false does not count). Opt out with ZMEM_ALLOW_REAL_STORE=1.
 *
 * Required by the JS test files that spawn store.py (mirrors the python
 * wiring); the C9 acceptance check drives it directly.
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

function candidatePaths() {
  const seen = new Set();
  const out = [];
  const push = (p) => {
    if (p && !seen.has(p.toLowerCase())) {
      seen.add(p.toLowerCase());
      out.push(p);
    }
  };
  for (const v of ["ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA",
                   "ZCODE_PLUGIN_DATA"]) {
    const raw = (process.env[v] || "").trim();
    if (!raw) continue;
    const p = path.resolve(raw.replace(/^~(?=$|[\\/])/, os.homedir()));
    push(p.endsWith(".sqlite") ? p : path.join(p, "store.sqlite"));
  }
  push(path.join(os.homedir(), ".zcode", "memory", "store.sqlite"));
  push(path.join(os.homedir(), ".zmem", "store.sqlite"));
  return out;
}

function resolvedStorePath() {
  /** The store path this environment WOULD use, by the same precedence
   * host.resolve_store_path applies (explicit vars in order, else the home
   * default). The guard judges THIS path — a scratch ZMEM_STORE/ZMEM_DATA
   * keeps the run green even though the default candidate still exists. */
  for (const v of ["ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA",
                   "ZCODE_PLUGIN_DATA"]) {
    const raw = (process.env[v] || "").trim();
    if (!raw) continue;
    const p = path.resolve(raw.replace(/^~(?=$|[\\/])/, os.homedir()));
    return p.endsWith(".sqlite") ? p : path.join(p, "store.sqlite");
  }
  return path.join(os.homedir(), ".zmem", "store.sqlite");
}

function check(failFast) {
  if ((process.env.ZMEM_ALLOW_REAL_STORE || "").trim() === "1") {
    return { ok: true, why: "opt-out ZMEM_ALLOW_REAL_STORE=1 set" };
  }
  // CI runners are ephemeral: the home dies with the job, and suites there
  // may create ~/.zmem/store.sqlite as a side effect (PR #410 CI round 4).
  // The guard protects DEVELOPER homes; CI-side fixture leakage is the
  // write-time refusal's job (always active).
  // Truthy VALUES only: CI=false (a developer convention) must not silence
  // the guard on a live home (final-critic round 4).
  const ci = (process.env.CI || "").trim().toLowerCase();
  const gha = (process.env.GITHUB_ACTIONS || "").trim().toLowerCase();
  if (["true", "1", "yes"].includes(ci) ||
      ["true", "1", "yes"].includes(gha)) {
    return { ok: true, why: "ephemeral CI runner (CI/GITHUB_ACTIONS truthy)" };
  }
  const root = path.join(os.homedir(), ".zmem");
  // Gate on the default STORE FILE, not the root dir: CI lanes can have
  // ~/.zmem created as a side effect while a live operator store always has
  // the file (PR #410 CI round).
  let storeExists = false;
  try {
    storeExists = fs.statSync(path.join(root, "store.sqlite")).isFile();
  } catch (_e) {
    storeExists = false;
  }
  if (!storeExists) {
    return { ok: true, why: `no real default store at ${root}` };
  }
  const resolved = resolvedStorePath();
  const rel = path.relative(root, resolved);
  if (rel === "" || (!rel.split(path.sep).includes("..") &&
                     !path.isAbsolute(rel))) {
    const msg =
      "tests/_store_guard.js: refusing to run against the REAL default " +
      `store area (${resolved} is inside ${root}). Set ZMEM_STORE (a ` +
      "sqlite FILE path) or ZMEM_DATA (a directory) to a scratch path, or " +
      "set ZMEM_ALLOW_REAL_STORE=1 to run deliberately against the live " +
      "store.";
    if (failFast) {
      console.error(msg);
      process.exit(1);
    }
    return { ok: false, why: msg };
  }
  return { ok: true, why: `resolved ${resolved} is outside the real root` };
}

if (require.main === module) {
  const r = check(true);
  console.log(`store-guard: ok=${r.ok} ${r.why}`);
  process.exit(0);
} else {
  module.exports = { check, candidatePaths, resolvedStorePath };
  check(true);
}
