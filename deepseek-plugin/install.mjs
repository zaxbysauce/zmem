// Install/upgrade/activation/uninstall state machine over a dsh profile
// root (issue #374 AC6). The dsh product loads plugins via a cordis.yml
// profile row {name, config} resolved by the harness's own loader (which
// is NOT part of the published @deepseek-ai packages) — this module writes
// and validates that row plus the activation state file. Disclosed
// boundary: validated at the profile-row/package-contract level, NOT
// against a running product loader. The shared store is NEVER touched on
// uninstall; user patch rows are preserved on upgrade.
import fs from "node:fs";
import path from "node:path";

export const PACKAGE_NAME = "@zaxbyhub/zmem-deepseek";
const ACTIVATION = "zmem-deepseek.activation.json";

function profileFile(profileRoot) {
  return path.join(profileRoot, "cordis.yml");
}

export function install(profileRoot, pluginDir) {
  const root = path.resolve(profileRoot);
  fs.mkdirSync(root, { recursive: true });
  const profile = path.join(root, "plugins", PACKAGE_NAME);
  // node_modules is NEVER copied: the dsh HOST provides the @deepseek-ai
  // runtime (peerDependencies), and cpSync chokes on node_modules/.bin
  // symlinks whose targets live inside the copied tree (EINVAL).
  fs.cpSync(path.resolve(pluginDir), profile, { recursive: true,
    filter: (src) => !src.includes(
      `${path.sep}node_modules`) });
  // The installed profile dir contains ONLY this package — record where
  // the zmem backend (skills/memory/scripts) actually lives so the
  // launcher's ROOT resolution works installed, not just from a repo
  // checkout. The source pluginDir's parent is the zmem plugin root in
  // both the repo and the marketplace-cache layout.
  const backendRoot = path.resolve(pluginDir, "..");
  if (fs.existsSync(path.join(backendRoot, "skills", "memory",
      "scripts", "store.py"))) {
    fs.writeFileSync(path.join(profile, "zmem-root.json"), JSON.stringify(
      { root: backendRoot }, null, 1) + "\n");
  }
  const yml = profileFile(root);
  const existing = fs.existsSync(yml) ? fs.readFileSync(yml, "utf8") : "";
  const row = `- name: ${PACKAGE_NAME}\n  config: {}\n`;
  if (!existing.includes(PACKAGE_NAME)) {
    fs.writeFileSync(yml, existing + (existing.endsWith("\n") || !existing ? "" : "\n") + row);
  }
  // Activation requires a host restart: no HMR is claimed (restartRequired
  // is reported honestly in the activation state file).
  const stateFile = path.join(root, ACTIVATION);
  const state = fs.existsSync(stateFile)
    ? JSON.parse(fs.readFileSync(stateFile, "utf8")) : {};
  fs.writeFileSync(stateFile, JSON.stringify({
    ...state, installed: true, restartRequired: true, pluginDir: profile,
  }, null, 1));
  return { ok: true, profileRow: PACKAGE_NAME, restartRequired: true };
}

export function upgrade(profileRoot, newPluginDir) {
  // User patch rows (files the operator added under the plugin dir that
  // this package does not own) are PRESERVED across the copy.
  const root = path.resolve(profileRoot);
  const profile = path.join(root, "plugins", PACKAGE_NAME);
  if (!fs.existsSync(profile)) return { ok: false, reason: "not-installed" };
  const shipped = new Set(["plugin.json", "package.json", "package-lock.json",
    "index.mjs", "launcher.mjs", "identity.mjs", "tools.mjs", "capture.mjs",
    "install.mjs", "canary.mjs", "node_modules"]);
  const preserved = fs.readdirSync(profile)
    .filter((f) => !shipped.has(f));
  fs.cpSync(path.resolve(newPluginDir), profile, { recursive: true,
    filter: (src) => !src.includes(
      `${path.sep}node_modules`) });
  // Re-record the backend-root pointer. cpSync MERGES, so a
  // destination-only pointer from the previous install SURVIVES the copy —
  // delete it first, then rewrite only when the new source actually has a
  // sibling backend; an upgrade whose source lacks the backend must not
  // silently retain the old binding while reporting success.
  const pointerPath = path.join(profile, "zmem-root.json");
  fs.rmSync(pointerPath, { force: true });
  const backendRoot = path.resolve(newPluginDir, "..");
  let backendRootStaged = false;
  if (fs.existsSync(path.join(backendRoot, "skills", "memory",
      "scripts", "store.py"))) {
    fs.writeFileSync(pointerPath, JSON.stringify(
      { root: backendRoot }, null, 1) + "\n");
    backendRootStaged = true;
  }
  const stateFile = path.join(root, ACTIVATION);
  fs.writeFileSync(stateFile, JSON.stringify({
    ...JSON.parse(fs.readFileSync(stateFile, "utf8") || "{}"),
    upgraded: true, restartRequired: true, preserved,
    backendRootStaged,
  }, null, 1));
  return { ok: true, preserved, restartRequired: true, backendRootStaged };
}

export function uninstall(profileRoot) {
  // Removes the plugin and its profile row ONLY; the shared memory store
  // lives under ZMEM_DATA and is retained by design.
  const root = path.resolve(profileRoot);
  const profile = path.join(root, "plugins", PACKAGE_NAME);
  if (fs.existsSync(profile)) fs.rmSync(profile, { recursive: true, force: true });
  const yml = profileFile(root);
  if (fs.existsSync(yml)) {
    const kept = fs.readFileSync(yml, "utf8")
      .split(/\r?\n/)
      .filter((l, i, arr) => {
        if (l.includes(PACKAGE_NAME)) return false;
        if (/^\s+config:/.test(l) && arr[i - 1]
            && arr[i - 1].includes(PACKAGE_NAME)) return false;
        return true;
      }).join("\n");
    fs.writeFileSync(yml, kept);
  }
  const stateFile = path.join(root, ACTIVATION);
  if (fs.existsSync(stateFile)) fs.rmSync(stateFile, { force: true });
  return { ok: true, storeRetained: true };
}
